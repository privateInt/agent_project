package app

import (
	"context"
	"errors"
	"fmt"
	"model-platform/internal/artifacts"
	"regexp"
	"strings"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	managementv1 "model-platform/gen/management/v1"
)

var sha256Hex = regexp.MustCompile(`^[0-9a-f]{64}$`)

func validUUID(value string) bool { _, err := uuid.Parse(value); return err == nil }

// Reserve a deterministic model version. The manifest digest fixes the bytes
// that may be published; a different retry cannot replace a completed model.
func (s *Service) RegisterTrainingOutput(ctx context.Context, req *managementv1.RegisterTrainingOutputRequest) (*managementv1.ModelVersion, error) {
	runID, sourceID, digest := strings.TrimSpace(req.GetTrainingRunId()), strings.TrimSpace(req.GetSourceModelVersionId()), strings.TrimSpace(req.GetManifestSha256())
	if !validUUID(runID) || !validUUID(sourceID) || !sha256Hex.MatchString(digest) {
		return nil, status.Error(codes.InvalidArgument, "valid training_run_id, source_model_version_id and manifest_sha256 are required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin output registration: %v", err)
	}
	defer tx.Rollback(ctx)
	// Serialize reservations for this run, including the insert race.
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1,0))`, runID); err != nil {
		return nil, status.Errorf(codes.Internal, "lock output registration: %v", err)
	}
	var sourceModelID, repoID, requested, resolved, baseURI, kind, sourceState string
	err = tx.QueryRow(ctx, `SELECT model_id,repo_id,requested_revision,resolved_revision,COALESCE(artifact_uri,''),artifact_kind,state FROM model_versions WHERE model_version_id=$1`, sourceID).Scan(&sourceModelID, &repoID, &requested, &resolved, &baseURI, &kind, &sourceState)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "source model version not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "load source model: %v", err)
	}
	var purging bool
	if err = tx.QueryRow(ctx, `SELECT purge_state<>'' FROM models WHERE model_id=$1 FOR UPDATE`, sourceModelID).Scan(&purging); err != nil {
		return nil, err
	}
	if purging {
		return nil, status.Error(codes.FailedPrecondition, "model is permanently deleted or deletion is pending")
	}
	if sourceState != "ready" || kind != "snapshot" || baseURI == "" {
		return nil, status.Error(codes.FailedPrecondition, "source must be a ready snapshot")
	}
	var id, existingSource, existingDigest, state string
	err = tx.QueryRow(ctx, `SELECT model_version_id,source_model_version_id,artifact_manifest_sha256,state FROM model_versions WHERE training_run_id=$1`, runID).Scan(&id, &existingSource, &existingDigest, &state)
	if err == nil {
		if existingSource != sourceID || existingDigest != digest {
			return nil, status.Error(codes.AlreadyExists, "training run already has a different output reservation")
		}
		if state != "importing" && state != "failed" && state != "ready" {
			return nil, status.Errorf(codes.FailedPrecondition, "output state is %s", state)
		}
		// A failed upload may be retried with the same content; ready is immutable.
		if state == "failed" {
			_, err = tx.Exec(ctx, `UPDATE model_versions SET state='importing',updated_at=NOW() WHERE model_version_id=$1 AND state='failed'`, id)
			if err != nil {
				return nil, status.Errorf(codes.Internal, "retry output registration: %v", err)
			}
		}
	} else if errors.Is(err, pgx.ErrNoRows) {
		id = uuid.NewString()
		// No mutable attempt directory is shared by different output contents.
		_, err = tx.Exec(ctx, `INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,source_ref,artifact_kind,repo_id,requested_revision,resolved_revision,training_run_id,source_model_version_id,base_model_artifact_uri,artifact_manifest_sha256)
  VALUES($1,$2,$3,'importing','training',jsonb_build_object('training_run_id',$4::text,'source_model_version_id',$5::text,'base_model_artifact_uri',$6::text),'adapter',$7,$8,$9,$4::uuid,$5::uuid,$6,$10)`, id, sourceModelID, "train-"+runID, runID, sourceID, baseURI, repoID, requested, resolved, digest)
		if err != nil {
			return nil, status.Errorf(codes.Internal, "reserve training output: %v", err)
		}
	} else {
		return nil, status.Errorf(codes.Internal, "lookup output reservation: %v", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit output registration: %v", err)
	}
	return s.getVersion(ctx, id)
}

func (s *Service) CompleteTrainingOutput(ctx context.Context, req *managementv1.CompleteTrainingOutputRequest) (*managementv1.ModelVersion, error) {
	id, uri, digest := strings.TrimSpace(req.GetModelVersionId()), strings.TrimSpace(req.GetArtifactUri()), strings.TrimSpace(req.GetManifestSha256())
	if !validUUID(id) || !sha256Hex.MatchString(digest) || req.GetArtifactSizeBytes() <= 0 || req.GetArtifactFileCount() <= 0 {
		return nil, status.Error(codes.InvalidArgument, "valid output id, manifest digest, positive file count and size are required")
	}
	if s.artifactStore == nil {
		return nil, status.Error(codes.FailedPrecondition, "artifact verifier is not configured")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin output completion: %v", err)
	}
	defer tx.Rollback(ctx)
	var runID, modelID, expectedDigest, state, oldURI, oldHash string
	var oldSize, oldCount int64
	err = tx.QueryRow(ctx, `SELECT training_run_id,model_id,artifact_manifest_sha256,state,COALESCE(artifact_uri,''),COALESCE(artifact_sha256,''),COALESCE(artifact_size_bytes,0),artifact_file_count FROM model_versions WHERE model_version_id=$1 AND source_type='training' AND artifact_kind='adapter' FOR UPDATE`, id).Scan(&runID, &modelID, &expectedDigest, &state, &oldURI, &oldHash, &oldSize, &oldCount)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "training output not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "load training output: %v", err)
	}
	prefix, prefixErr := artifacts.Prefix(modelID, id, digest)
	if prefixErr != nil {
		return nil, status.Errorf(codes.Internal, "invalid reserved output identity: %v", prefixErr)
	}
	expectedURI := fmt.Sprintf("s3://%s/%s", s.artifactBucket, prefix)
	if digest != expectedDigest || uri != expectedURI {
		return nil, status.Error(codes.FailedPrecondition, "output URI or manifest does not match reservation")
	}
	// This is a server-side readback, not a claim supplied by the Training Job.
	verified, verifyErr := s.artifactStore.VerifyManifest(ctx, prefix, digest)
	if verifyErr != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "verify remote training artifact: %v", verifyErr)
	}
	if verified.TotalBytes != req.GetArtifactSizeBytes() || int64(verified.FileCount) != req.GetArtifactFileCount() {
		return nil, status.Error(codes.FailedPrecondition, "verified artifact size or file count differs from request")
	}
	// The aggregate artifact digest is the SHA256 of the canonical manifest.
	if state == "ready" {
		if oldURI != uri || oldHash != digest || oldSize != req.GetArtifactSizeBytes() || oldCount != req.GetArtifactFileCount() {
			return nil, status.Error(codes.AlreadyExists, "ready output is immutable")
		}
	} else if state == "importing" {
		_, err = tx.Exec(ctx, `UPDATE model_versions SET state='ready',artifact_uri=$2,artifact_sha256=$3,artifact_size_bytes=$4,artifact_file_count=$5,updated_at=NOW() WHERE model_version_id=$1 AND state='importing'`, id, uri, digest, req.GetArtifactSizeBytes(), req.GetArtifactFileCount())
		if err != nil {
			return nil, status.Errorf(codes.Internal, "complete training output: %v", err)
		}
	} else {
		return nil, status.Errorf(codes.FailedPrecondition, "output state is %s", state)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit output completion: %v", err)
	}
	return s.getVersion(ctx, id)
}

func (s *Service) FailTrainingOutput(ctx context.Context, req *managementv1.FailTrainingOutputRequest) (*managementv1.ModelVersion, error) {
	id := strings.TrimSpace(req.GetModelVersionId())
	if !validUUID(id) {
		return nil, status.Error(codes.InvalidArgument, "valid model_version_id is required")
	}
	tag, err := s.pool.Exec(ctx, `UPDATE model_versions SET state='failed',updated_at=NOW() WHERE model_version_id=$1 AND source_type='training' AND state='importing'`, id)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "fail training output: %v", err)
	}
	if tag.RowsAffected() == 0 {
		mv, e := s.getVersion(ctx, id)
		if e != nil {
			return nil, e
		}
		if mv.GetState() != "failed" && mv.GetState() != "ready" {
			return nil, status.Error(codes.FailedPrecondition, "output cannot be failed")
		}
		return mv, nil
	}
	return s.getVersion(ctx, id)
}
