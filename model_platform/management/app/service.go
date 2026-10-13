package app

import (
	"context"
	"errors"
	"strings"
	"time"

	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/storage"
	"model-platform/internal/submission"
	"model-platform/management/grpcclient"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Service struct {
	purgeDeployment deploymentv1.DeploymentServiceClient
	purgeTraining   trainingv1.TrainingServiceClient
	modelFiles      interface {
		PurgeModel(context.Context, string) error
	}
	managementv1.UnimplementedManagementServiceServer
	pool           *pgxpool.Pool
	operations     operationCreator
	credentialKey  []byte
	artifactBucket string
	artifactStore  *storage.MinIO
}

func New(pool *pgxpool.Pool, operations *grpcclient.Operation, credentialKey []byte, artifactStore *storage.MinIO) *Service {
	return &Service{pool: pool, operations: operations, credentialKey: credentialKey, artifactBucket: artifactStore.Bucket(), artifactStore: artifactStore, modelFiles: artifactStore}
}

func InitSchema(ctx context.Context, pool *pgxpool.Pool) error {
	tx, err := pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer tx.Rollback(ctx)

	_, err = tx.Exec(ctx, `
CREATE TABLE IF NOT EXISTS models (
    model_id UUID PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    model_type VARCHAR(32) NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE models ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE models ADD COLUMN IF NOT EXISTS purge_state TEXT NOT NULL DEFAULT '';
ALTER TABLE models ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS models_browse_idx ON models(created_at DESC,model_id DESC);
ALTER TABLE models
    ADD COLUMN IF NOT EXISTS model_type VARCHAR(32);
ALTER TABLE models
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
UPDATE models
SET model_type = 'llm'
WHERE model_type IS NULL OR BTRIM(model_type) = '';
ALTER TABLE models
    ALTER COLUMN model_type SET NOT NULL;
ALTER TABLE models
    DROP CONSTRAINT IF EXISTS models_model_type_check;
ALTER TABLE models
    ADD CONSTRAINT models_model_type_check
    CHECK (model_type IN ('llm','embedding','reranker','ocr'));

CREATE TABLE IF NOT EXISTS model_versions (
    model_version_id UUID PRIMARY KEY,
    model_id UUID NOT NULL REFERENCES models(model_id) ON DELETE RESTRICT,
    version VARCHAR(128) NOT NULL,
    state VARCHAR(32) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_ref JSONB NOT NULL DEFAULT '{}'::jsonb,
    artifact_kind VARCHAR(32) NOT NULL DEFAULT 'snapshot',
    training_run_id UUID,
    source_model_version_id UUID,
    base_model_artifact_uri TEXT NOT NULL DEFAULT '',
    artifact_manifest_sha256 VARCHAR(64) NOT NULL DEFAULT '',
    artifact_file_count BIGINT NOT NULL DEFAULT 0,
    repo_id TEXT NOT NULL DEFAULT '',
    requested_revision TEXT NOT NULL DEFAULT '',
    resolved_revision TEXT NOT NULL DEFAULT '',
    artifact_uri TEXT,
    artifact_sha256 VARCHAR(64),
    artifact_size_bytes BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(model_id, version)
);

ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS training_run_id UUID;
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS source_model_version_id UUID;
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS base_model_artifact_uri TEXT NOT NULL DEFAULT '';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS artifact_manifest_sha256 VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS artifact_file_count BIGINT NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS model_versions_training_run_idx ON model_versions(training_run_id) WHERE training_run_id IS NOT NULL;
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS source_ref JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS artifact_kind VARCHAR(32) NOT NULL DEFAULT 'snapshot';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS repo_id TEXT NOT NULL DEFAULT '';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS requested_revision TEXT NOT NULL DEFAULT '';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS resolved_revision TEXT NOT NULL DEFAULT '';
ALTER TABLE model_versions
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

ALTER TABLE model_versions
    ALTER COLUMN artifact_uri DROP NOT NULL;
ALTER TABLE model_versions
    ALTER COLUMN artifact_sha256 DROP NOT NULL;
ALTER TABLE model_versions
    ALTER COLUMN artifact_size_bytes DROP NOT NULL;

ALTER TABLE model_versions
    DROP CONSTRAINT IF EXISTS model_versions_state_check;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_state_check
    CHECK (state IN ('importing','ready','failed','deleting','deleted'));

ALTER TABLE model_versions
    DROP CONSTRAINT IF EXISTS model_versions_source_type_check;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_source_type_check
    CHECK (source_type IN ('upload','huggingface','training'));

-- Check legacy values before changing them. Never guess the meaning of an
-- unknown artifact kind. This block is part of the schema transaction.
DO $artifact_kind_migration$
DECLARE
    invalid_rows TEXT;
BEGIN
    SELECT string_agg(
        format('id=%s source_type=%s artifact_kind=%s',
            model_version_id, source_type, COALESCE(quote_nullable(artifact_kind), 'NULL')),
        E'\n' ORDER BY model_version_id
    ) INTO invalid_rows
    FROM (
        SELECT model_version_id, source_type, artifact_kind
        FROM model_versions
        WHERE NOT (
            COALESCE(LOWER(BTRIM(artifact_kind)), '') IN ('file','snapshot','adapter')
            OR (
                (artifact_kind IS NULL OR BTRIM(artifact_kind) = '')
                AND source_type IN ('huggingface','upload')
            )
        )
        ORDER BY model_version_id
        LIMIT 20
    ) AS invalid;

    IF invalid_rows IS NOT NULL THEN
        RAISE EXCEPTION 'unsupported model_versions.artifact_kind values:\n%', invalid_rows
            USING ERRCODE = '23514',
                  HINT = 'Inspect the listed rows and choose an explicit mapping. No data was changed.';
    END IF;
END
$artifact_kind_migration$;

-- Drop the legacy constraint inside the transaction so normalization can
-- proceed even when the old constraint allowed a different spelling.
ALTER TABLE model_versions
    DROP CONSTRAINT IF EXISTS model_versions_artifact_kind_check;

-- Normalize known values; infer snapshot only for legacy import sources.
-- Unknown values and blank training artifacts are deliberately rejected.
UPDATE model_versions
SET artifact_kind = CASE
    WHEN artifact_kind IS NULL OR BTRIM(artifact_kind) = '' THEN 'snapshot'
    ELSE LOWER(BTRIM(artifact_kind))
END
WHERE artifact_kind IS DISTINCT FROM CASE
    WHEN artifact_kind IS NULL OR BTRIM(artifact_kind) = '' THEN 'snapshot'
    ELSE LOWER(BTRIM(artifact_kind))
END;

ALTER TABLE model_versions
    ALTER COLUMN artifact_kind SET DEFAULT 'snapshot';
ALTER TABLE model_versions
    ALTER COLUMN artifact_kind SET NOT NULL;
ALTER TABLE model_versions
    ADD CONSTRAINT model_versions_artifact_kind_check
    CHECK (artifact_kind IN ('file','snapshot','adapter'));

CREATE INDEX IF NOT EXISTS model_versions_browse_idx ON model_versions(created_at DESC,model_version_id DESC);
CREATE INDEX IF NOT EXISTS model_versions_model_idx
    ON model_versions(model_id, created_at DESC);

-- User credentials are intentionally separated from model metadata.
-- They are encrypted with the platform key and automatically expire.
CREATE TABLE IF NOT EXISTS import_credentials (
    model_version_id UUID PRIMARY KEY REFERENCES model_versions(model_version_id) ON DELETE CASCADE,
    credential_type VARCHAR(32) NOT NULL DEFAULT 'huggingface_token',
    nonce BYTEA NOT NULL,
    ciphertext BYTEA NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT import_credentials_type_check CHECK (credential_type IN ('huggingface_token'))
);
CREATE INDEX IF NOT EXISTS import_credentials_expires_idx ON import_credentials(expires_at);
ALTER TABLE model_versions ADD COLUMN IF NOT EXISTS import_credential_required BOOLEAN NOT NULL DEFAULT FALSE;
UPDATE model_versions SET import_credential_required=TRUE WHERE NOT import_credential_required AND model_version_id IN (SELECT model_version_id FROM import_credentials);
DELETE FROM import_credentials WHERE expires_at <= NOW();
`+idempotency.Schema+submission.Schema)
	if err != nil {
		return err
	}
	return tx.Commit(ctx)
}
func (s *Service) GetModel(ctx context.Context, req *managementv1.GetModelRequest) (*managementv1.Model, error) {
	id := strings.TrimSpace(req.GetModelId())
	var name, modelType, description string
	var createdAt, updatedAt time.Time
	var deletedAt, purgedAt *time.Time
	var purgeState string
	err := s.pool.QueryRow(ctx, `
SELECT name,model_type,description,created_at,updated_at,deleted_at,purge_state,purged_at
FROM models
WHERE model_id=$1
`, id).Scan(&name, &modelType, &description, &createdAt, &updatedAt, &deletedAt, &purgeState, &purgedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "model not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get model: %v", err)
	}
	var removed, purged *timestamppb.Timestamp
	if purgedAt != nil {
		purged = timestamppb.New(*purgedAt)
	}
	if deletedAt != nil {
		removed = timestamppb.New(*deletedAt)
	}
	return &managementv1.Model{
		PurgeState: purgeState, PurgedAt: purged, DeletedAt: removed, ModelId: id, Name: name, ModelType: modelType, Description: description,
		CreatedAt: timestamppb.New(createdAt), UpdatedAt: timestamppb.New(updatedAt),
	}, nil
}

func (s *Service) GetModelVersion(ctx context.Context, req *managementv1.GetModelVersionRequest) (*managementv1.ModelVersion, error) {
	return s.getVersion(ctx, strings.TrimSpace(req.GetModelVersionId()))
}

func (s *Service) GetImportSpec(ctx context.Context, req *managementv1.GetImportSpecRequest) (*managementv1.ImportSpec, error) {
	id := strings.TrimSpace(req.GetModelVersionId())
	var modelID, repoID, revision, state string
	err := s.pool.QueryRow(ctx, `SELECT model_id,repo_id,requested_revision,state FROM model_versions WHERE model_version_id=$1`, id).Scan(&modelID, &repoID, &revision, &state)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "model version not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get import spec: %v", err)
	}
	if state != "importing" {
		return nil, status.Errorf(codes.FailedPrecondition, "model version state is %s", state)
	}
	return &managementv1.ImportSpec{ModelVersionId: id, ModelId: modelID, RepoId: repoID, RequestedRevision: revision}, nil
}

// GetImportCredential is an internal-only lookup used by the one-shot Job.
//
// [FLOW-15] Job -> Management credential boundary.
// Public repositories have no row and therefore return an empty token. Private
// repositories return the decrypted token only while the short TTL is valid.
func (s *Service) GetImportCredential(ctx context.Context, req *managementv1.GetImportCredentialRequest) (*managementv1.ImportCredential, error) {
	id := strings.TrimSpace(req.GetModelVersionId())
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "model_version_id is required")
	}

	var nonce, ciphertext []byte
	var expiresAt time.Time
	err := s.pool.QueryRow(ctx, `
SELECT nonce,ciphertext,expires_at
FROM import_credentials
WHERE model_version_id=$1 AND credential_type='huggingface_token'
`, id).Scan(&nonce, &ciphertext, &expiresAt)
	if errors.Is(err, pgx.ErrNoRows) {
		var required bool
		e := s.pool.QueryRow(ctx, `SELECT import_credential_required FROM model_versions WHERE model_version_id=$1`, id).Scan(&required)
		if errors.Is(e, pgx.ErrNoRows) {
			return nil, status.Error(codes.NotFound, "model version not found")
		}
		if e != nil {
			return nil, idempotency.DBError(e)
		}
		if required {
			return nil, status.Error(codes.FailedPrecondition, "hugging face credential expired or unavailable")
		}
		return &managementv1.ImportCredential{}, nil
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get import credential: %v", err)
	}
	if !expiresAt.After(time.Now().UTC()) {
		_, _ = s.pool.Exec(ctx, `DELETE FROM import_credentials WHERE model_version_id=$1`, id)
		return nil, status.Error(codes.FailedPrecondition, "hugging face credential expired")
	}

	token, err := decryptCredential(s.credentialKey, nonce, ciphertext)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "decrypt import credential: %v", err)
	}
	return &managementv1.ImportCredential{HfToken: token}, nil
}

// CompleteImport finalizes domain state and destroys the temporary credential
// in the same DB transaction.
func (s *Service) CompleteImport(ctx context.Context, req *managementv1.CompleteImportRequest) (*managementv1.ModelVersion, error) {
	id := strings.TrimSpace(req.GetModelVersionId())
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin complete import: %v", err)
	}
	defer tx.Rollback(ctx)
	tag, err := tx.Exec(ctx, `
UPDATE model_versions
SET state='ready',
    resolved_revision=$2,
    source_ref = COALESCE(source_ref, '{}'::jsonb) || jsonb_build_object('resolved_revision',$2::text),
    artifact_kind='snapshot',
    artifact_uri=$3,
    artifact_sha256=$4,
    artifact_size_bytes=$5,
    updated_at=NOW()
WHERE model_version_id=$1 AND state='importing' AND source_type='huggingface'
`, id, req.GetResolvedRevision(), req.GetArtifactUri(), req.GetArtifactSha256(), req.GetArtifactSizeBytes())
	if err != nil {
		return nil, status.Errorf(codes.Internal, "complete import: %v", err)
	}
	if tag.RowsAffected() != 1 {
		return nil, status.Error(codes.FailedPrecondition, "import is not in importing state")
	}
	if _, err := tx.Exec(ctx, `DELETE FROM import_credentials WHERE model_version_id=$1`, id); err != nil {
		return nil, status.Errorf(codes.Internal, "delete import credential: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit complete import: %v", err)
	}
	return s.getVersion(ctx, id)
}

// FailImport marks the domain resource failed and removes any user credential.
func (s *Service) FailImport(ctx context.Context, req *managementv1.FailImportRequest) (*managementv1.ModelVersion, error) {
	id := strings.TrimSpace(req.GetModelVersionId())
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin fail import: %v", err)
	}
	defer tx.Rollback(ctx)
	_, err = tx.Exec(ctx, `UPDATE model_versions SET state='failed',updated_at=NOW() WHERE model_version_id=$1 AND state='importing'`, id)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "fail import: %v", err)
	}
	if _, err := tx.Exec(ctx, `DELETE FROM import_credentials WHERE model_version_id=$1`, id); err != nil {
		return nil, status.Errorf(codes.Internal, "delete import credential: %v", err)
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit fail import: %v", err)
	}
	return s.getVersion(ctx, id)
}

func (s *Service) getVersion(ctx context.Context, id string) (*managementv1.ModelVersion, error) {
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "model_version_id is required")
	}
	var modelID, version, state, sourceType, repoID, requested, resolved, uri, sha, kind, runID, parentID, baseURI, manifestSHA string
	var size, fileCount int64
	var createdAt, updatedAt time.Time
	err := s.pool.QueryRow(ctx, `SELECT model_id,version,state,source_type,repo_id,requested_revision,resolved_revision,
       COALESCE(artifact_uri,''),COALESCE(artifact_sha256,''),COALESCE(artifact_size_bytes,0),
       artifact_kind,COALESCE(training_run_id::text,''),COALESCE(source_model_version_id::text,''),base_model_artifact_uri,artifact_manifest_sha256,artifact_file_count,
       created_at,updated_at
FROM model_versions WHERE model_version_id=$1`, id).Scan(&modelID, &version, &state, &sourceType, &repoID, &requested, &resolved, &uri, &sha, &size, &kind, &runID, &parentID, &baseURI, &manifestSHA, &fileCount, &createdAt, &updatedAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "model version not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get model version: %v", err)
	}
	info, err := submission.Lookup(ctx, s.pool, "model_version", id)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	return &managementv1.ModelVersion{OperationId: info.OperationID, SubmissionStatus: info.Status, SubmissionErrorCode: info.ErrorCode, ModelVersionId: id, ModelId: modelID, Version: version, State: state, SourceType: sourceType, RepoId: repoID, RequestedRevision: requested, ResolvedRevision: resolved, ArtifactUri: uri, ArtifactSha256: sha, ArtifactSizeBytes: size, ArtifactKind: kind, TrainingRunId: runID, SourceModelVersionId: parentID, BaseModelArtifactUri: baseURI, ArtifactManifestSha256: manifestSHA, ArtifactFileCount: fileCount, CreatedAt: timestamppb.New(createdAt), UpdatedAt: timestamppb.New(updatedAt)}, nil
}
