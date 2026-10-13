package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	ocrv1 "data-platform/gen/ocr/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/canonical"
	"data-platform/internal/idempotency"
	"data-platform/internal/listing"
	"data-platform/internal/ocr"
	"data-platform/internal/sourcefile"
	"data-platform/internal/submission"
	"encoding/json"
	"errors"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"strings"
	"time"
)

const Schema = `
CREATE TABLE IF NOT EXISTS refinement_runs (
 refinement_run_id UUID PRIMARY KEY, operation_id UUID NOT NULL UNIQUE,
 name TEXT NOT NULL, source_document_id UUID NOT NULL, source_sha256 VARCHAR(64) NOT NULL,
 source_format TEXT NOT NULL, rules_version TEXT NOT NULL,
 output_document_id UUID UNIQUE, completion_hash TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS canonical_documents (
 document_id UUID PRIMARY KEY, refinement_run_id UUID NOT NULL UNIQUE REFERENCES refinement_runs(refinement_run_id),
 source_document_id UUID NOT NULL, sha256 VARCHAR(64) NOT NULL,
 size_bytes BIGINT NOT NULL CHECK(size_bytes>0 AND size_bytes<=33554432), object_key TEXT NOT NULL UNIQUE,
 state TEXT NOT NULL CHECK(state IN ('ready','needs_ocr','needs_review')),
 schema_version TEXT NOT NULL, processor_version TEXT NOT NULL, rules_version TEXT NOT NULL,
 processor_image_id TEXT NOT NULL, block_count INTEGER NOT NULL, page_count INTEGER NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE refinement_runs ADD COLUMN IF NOT EXISTS ocr_json TEXT NOT NULL DEFAULT '';
ALTER TABLE refinement_runs ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE refinement_runs ADD COLUMN IF NOT EXISTS purge_state TEXT NOT NULL DEFAULT '';
ALTER TABLE refinement_runs ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS source_purge_fences(document_id UUID PRIMARY KEY);
CREATE INDEX IF NOT EXISTS refinement_runs_browse_idx ON refinement_runs(created_at DESC,refinement_run_id DESC);
`

type Service struct {
	refinementv1.UnimplementedRefinementServiceServer
	pool       *pgxpool.Pool
	ops        operationv1.OperationServiceClient
	collection collectionv1.CollectionServiceClient
	store      blob.Store
	reads      chan struct{}
	ocrClient  ocrv1.OCRDeploymentServiceClient
}

func New(p *pgxpool.Pool, o operationv1.OperationServiceClient, c collectionv1.CollectionServiceClient, b blob.Store) *Service {
	return &Service{pool: p, ops: o, collection: c, store: b, reads: make(chan struct{}, 2)}
}
func InitSchema(ctx context.Context, p *pgxpool.Pool) error {
	_, e := p.Exec(ctx, Schema+knowledgeReferenceSchema+idempotency.Schema+submission.Schema)
	return e
}
func (s *Service) RunSubmissions(ctx context.Context) {
	submission.Worker{Pool: s.pool, Ensure: func(c context.Context, r *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
		return s.ops.EnsureOperation(c, r)
	}}.Run(ctx)
}
func validID(v string) error {
	u, e := uuid.Parse(v)
	if e != nil || u == uuid.Nil || u.String() != v {
		return status.Error(codes.InvalidArgument, "canonical nonzero UUID required")
	}
	return nil
}
func readError(e error) error {
	if errors.Is(e, pgx.ErrNoRows) {
		return status.Error(codes.NotFound, "resource not found")
	}
	return idempotency.DBError(e)
}
func NormalizeRequest(r *refinementv1.CreateRunRequest) (*refinementv1.CreateRunRequest, error) {
	name, e := sourcefile.Name(r.GetName())
	if e != nil {
		return nil, status.Error(codes.InvalidArgument, e.Error())
	}
	if e = validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	if r.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key is required")
	}
	if e = idempotency.ValidateKey(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	rules := r.GetRulesVersion()
	if rules == "" {
		rules = canonical.Rules
	}
	if rules != canonical.Rules {
		return nil, status.Error(codes.InvalidArgument, "unsupported rules_version")
	}
	if r.GetOcrDeploymentId() != "" {
		if e = validID(r.GetOcrDeploymentId()); e != nil {
			return nil, e
		}
	}
	return &refinementv1.CreateRunRequest{OcrDeploymentId: r.GetOcrDeploymentId(), Name: name, DocumentId: r.DocumentId, RulesVersion: rules, IdempotencyKey: r.IdempotencyKey}, nil
}
func (s *Service) createRun(ctx context.Context, r *refinementv1.CreateRunRequest) (*refinementv1.RunAccepted, error) {
	n, e := NormalizeRequest(r)
	if e != nil {
		return nil, e
	}
	fingerprint, e := idempotency.Fingerprint(struct{ Version, Name, Document, Rules string }{"refinement-request-v1", n.Name, n.DocumentId, n.RulesVersion}, nil)
	if e != nil {
		return nil, e
	}
	if n.OcrDeploymentId != "" {
		fingerprint, e = idempotency.Fingerprint(struct{ Version, Name, Document, Rules, OCR string }{"refinement-ocr-request-v1", n.Name, n.DocumentId, n.RulesVersion, n.OcrDeploymentId}, nil)
		if e != nil {
			return nil, e
		}
	}
	out := &refinementv1.RunAccepted{RefinementRunId: uuid.NewString(), OperationId: uuid.NewString()}
	e = idempotency.Execute(ctx, s.pool, "refinement.parse", n.IdempotencyKey, fingerprint, out, func(ctx context.Context, tx pgx.Tx) error {
		if e := sourcePurgeLock(ctx, tx, n.DocumentId); e != nil {
			return e
		}
		var blocked bool
		if e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM source_purge_fences WHERE document_id=$1)`, n.DocumentId).Scan(&blocked); e != nil {
			return e
		}
		if blocked {
			return status.Error(codes.FailedPrecondition, "source is permanently deleted or deletion is pending")
		}
		// Only a new logical request reads Collection. Replays survive its outage.
		c, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		doc, e := s.collection.GetDocument(c, &collectionv1.GetDocumentRequest{DocumentId: n.DocumentId})
		if e != nil {
			return e
		}
		if doc.GetDocumentId() != n.DocumentId || doc.GetState() != "ready" || doc.GetDeletedAt() != nil || !blob.ValidDigest(doc.GetSha256()) || doc.GetSizeBytes() < 1 || doc.GetSizeBytes() > canonical.MaxBytes {
			return status.Error(codes.FailedPrecondition, "source document is not ready")
		}
		format, e := canonical.Format(doc)
		if e != nil {
			return status.Error(codes.InvalidArgument, e.Error())
		}
		ocrJSON := ""
		if n.OcrDeploymentId != "" {
			if format != "png" && format != "jpeg" && format != "pdf" {
				return status.Error(codes.InvalidArgument, "OCR currently accepts PNG, JPEG and PDF sources")
			}
			identity, err := ocr.Resolve(c, s.ocrClient, n.OcrDeploymentId)
			if err != nil {
				return status.Error(codes.FailedPrecondition, "OCR deployment is unavailable or not ready; verify its Surya profile")
			}
			encoded, _ := json.Marshal(identity)
			ocrJSON = string(encoded)
		}
		_, e = tx.Exec(ctx, `INSERT INTO refinement_runs(refinement_run_id,operation_id,name,source_document_id,source_sha256,source_format,rules_version,ocr_json) VALUES($1,$2,$3,$4,$5,$6,$7,$8)`, out.RefinementRunId, out.OperationId, n.Name, n.DocumentId, doc.Sha256, format, n.RulesVersion, ocrJSON)
		if e != nil {
			return e
		}
		return submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: out.OperationId, Kind: "refinement.parse", ExecutionType: "refinement.parse", ResourceType: "refinement_run", ResourceId: out.RefinementRunId, MaxAttempts: 1})
	})
	return out, e
}

const runSelect = `SELECT refinement_run_id::text,operation_id::text,name,source_document_id::text,source_sha256,source_format,rules_version,COALESCE(output_document_id::text,''),created_at,ocr_json,deleted_at,purge_state,purged_at FROM refinement_runs`

func scanRun(row pgx.Row) (*refinementv1.RefinementRun, error) {
	r := &refinementv1.RefinementRun{}
	var t time.Time
	var deleted, purged *time.Time
	e := row.Scan(&r.RefinementRunId, &r.OperationId, &r.Name, &r.SourceDocumentId, &r.SourceSha256, &r.SourceFormat, &r.RulesVersion, &r.OutputDocumentId, &t, &r.OcrJson, &deleted, &r.PurgeState, &purged)
	if e != nil {
		return nil, e
	}
	r.CreatedAt = timestamppb.New(t)
	if purged != nil {
		r.PurgedAt = timestamppb.New(*purged)
	}
	if deleted != nil {
		r.DeletedAt = timestamppb.New(*deleted)
	}
	return r, nil
}
func (s *Service) GetRun(ctx context.Context, r *refinementv1.GetRunRequest) (*refinementv1.RefinementRun, error) {
	if e := validID(r.GetRefinementRunId()); e != nil {
		return nil, e
	}
	run, e := scanRun(s.pool.QueryRow(ctx, runSelect+` WHERE refinement_run_id=$1`, r.RefinementRunId))
	if e != nil {
		return nil, readError(e)
	}
	info, e := submission.Lookup(ctx, s.pool, "refinement_run", run.RefinementRunId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	run.SubmissionStatus = info.Status
	run.SubmissionErrorCode = info.ErrorCode
	return run, nil
}
func (s *Service) ListRuns(ctx context.Context, r *refinementv1.ListRunsRequest) (*refinementv1.ListRunsResponse, error) {
	sourceID, e := listing.UUID(r.GetSourceDocumentId(), "source_document_id")
	if e != nil { return nil, e }
	q, e := listing.Text(r.GetQ())
	if e != nil {
		return nil, e
	}
	filters := []string{q}
	if sourceID != "" { filters = append(filters, "source:"+sourceID) }
	if r.GetIncludeDeleted() {
		filters = append(filters, "include-deleted")
	}
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "refinement-runs", filters...)
	if e != nil {
		return nil, e
	}
	query := listing.Query{}
	if !r.GetIncludeDeleted() {
		query.IsNull("deleted_at")
	}
	query.IsNull("purged_at")
	query.Equal("source_document_id", sourceID)
	query.Contains(q, "name")
	suffix := query.Finish(page, "created_at", "refinement_run_id")
	rows, e := s.pool.Query(ctx, runSelect+suffix, query.Args...)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer rows.Close()
	out := &refinementv1.ListRunsResponse{}
	for rows.Next() {
		if len(out.Items) == page.Limit {
			last := out.Items[len(out.Items)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.RefinementRunId)
			break
		}
		run, e := scanRun(rows)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
		out.Items = append(out.Items, run)
	}
	return out, idempotency.DBError(rows.Err())
}
func authorized(op *operationv1.Operation, r *refinementv1.CompleteRunRequest) bool {
	return op != nil && op.GetOperationId() == r.GetOperationId() && op.GetResourceId() == r.GetRefinementRunId() && op.GetResourceType() == "refinement_run" && op.GetExecutionType() == "refinement.parse" && op.GetDispatchProtocol() == 1 && op.GetExecutionStartedAt() != nil && op.GetDispatchToken() == r.GetDispatchToken() && op.GetExecutionInstanceId() == r.GetExecutionInstanceId() && (op.GetStatus() == "running" || op.GetStatus() == "succeeded")
}
func (s *Service) CompleteRun(ctx context.Context, r *refinementv1.CompleteRunRequest) (*refinementv1.RefinementRun, error) {
	for _, id := range []string{r.GetRefinementRunId(), r.GetOperationId(), r.GetDispatchToken(), r.GetExecutionInstanceId()} {
		if e := validID(id); e != nil {
			return nil, e
		}
	}
	if !blob.ValidDigest(r.GetSha256()) || r.GetSizeBytes() < 1 || r.GetSizeBytes() > canonical.MaxBytes || !strings.HasPrefix(r.GetProcessorImageId(), "sha256:") || !blob.ValidDigest(strings.TrimPrefix(r.GetProcessorImageId(), "sha256:")) {
		return nil, status.Error(codes.InvalidArgument, "invalid canonical result")
	}
	check := func() error {
		c, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		op, e := s.ops.GetOperation(c, &operationv1.GetOperationRequest{OperationId: r.OperationId})
		if e != nil {
			return e
		}
		if !authorized(op, r) {
			return status.Error(codes.FailedPrecondition, "result is not from the authorized refinement process")
		}
		return nil
	}
	if e := check(); e != nil {
		return nil, e
	}
	fingerprint, e := idempotency.Fingerprint(struct {
		SHA, Image string
		Size       int64
	}{r.Sha256, r.ProcessorImageId, r.SizeBytes}, nil)
	if e != nil {
		return nil, e
	}
	// Bound concurrent output buffering and JSON validation, including completions.
	select {
	case s.reads <- struct{}{}:
		defer func() { <-s.reads }()
	case <-ctx.Done():
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	run, e := scanRun(tx.QueryRow(ctx, runSelect+` WHERE refinement_run_id=$1 FOR UPDATE`, r.RefinementRunId))
	if e != nil {
		return nil, readError(e)
	}
	if run.PurgeState != "" {
		return nil, status.Error(codes.FailedPrecondition, "result is permanently deleted or deletion is pending")
	}
	if run.OperationId != r.OperationId {
		return nil, status.Error(codes.FailedPrecondition, "operation does not match refinement request")
	}
	if run.OutputDocumentId != "" {
		var previous string
		if e = tx.QueryRow(ctx, `SELECT completion_hash FROM refinement_runs WHERE refinement_run_id=$1`, run.RefinementRunId).Scan(&previous); e != nil {
			return nil, idempotency.DBError(e)
		}
		if previous != fingerprint {
			return nil, status.Error(codes.AlreadyExists, "immutable refinement result differs")
		}
		return run, nil
	}
	key, _ := canonical.Key(run.RefinementRunId, r.Sha256)
	raw, e := canonical.Read(ctx, s.store, key, r.Sha256, r.SizeBytes)
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, "stored canonical integrity verification failed")
	}
	ocrIdentity, e := ocr.Decode(run.OcrJson)
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, "stored OCR identity is invalid")
	}
	doc, e := canonical.Validate(raw, canonical.Spec{OCR: ocrIdentity, DocumentID: run.SourceDocumentId, SHA256: run.SourceSha256, Format: run.SourceFormat, RulesVersion: run.RulesVersion, ImageID: r.ProcessorImageId})
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, e.Error())
	}
	if e = check(); e != nil {
		return nil, e
	}
	id := uuid.NewString()
	_, e = tx.Exec(ctx, `INSERT INTO canonical_documents(document_id,refinement_run_id,source_document_id,sha256,size_bytes,object_key,state,schema_version,processor_version,rules_version,processor_image_id,block_count,page_count) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`, id, run.RefinementRunId, run.SourceDocumentId, r.Sha256, r.SizeBytes, key, doc.Quality.State, canonical.Schema, doc.Processor.Version, run.RulesVersion, r.ProcessorImageId, len(doc.Blocks), len(doc.Pages))
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	_, e = tx.Exec(ctx, `UPDATE refinement_runs SET output_document_id=$2,completion_hash=$3 WHERE refinement_run_id=$1`, run.RefinementRunId, id, fingerprint)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	run.OutputDocumentId = id
	return run, nil
}

const docSelect = `SELECT document_id::text,refinement_run_id::text,source_document_id::text,sha256,size_bytes,object_key,state,schema_version,processor_version,rules_version,processor_image_id,block_count,page_count,created_at FROM canonical_documents`

func (s *Service) GetDocument(ctx context.Context, r *refinementv1.GetDocumentRequest) (*refinementv1.CanonicalDocument, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	d := &refinementv1.CanonicalDocument{}
	var key string
	var t time.Time
	e := s.pool.QueryRow(ctx, docSelect+` WHERE document_id=$1`, r.DocumentId).Scan(&d.DocumentId, &d.RefinementRunId, &d.SourceDocumentId, &d.Sha256, &d.SizeBytes, &key, &d.State, &d.SchemaVersion, &d.ProcessorVersion, &d.RulesVersion, &d.ProcessorImageId, &d.BlockCount, &d.PageCount, &t)
	if e != nil {
		return nil, readError(e)
	}
	var purgeState string
	if e = s.pool.QueryRow(ctx, `SELECT purge_state FROM refinement_runs WHERE refinement_run_id=$1`, d.RefinementRunId).Scan(&purgeState); e != nil {
		return nil, readError(e)
	}
	if purgeState != "" {
		return nil, status.Error(codes.FailedPrecondition, "canonical file is permanently deleted or deletion is pending")
	}
	d.ArtifactUri = s.store.URI(key)
	d.CreatedAt = timestamppb.New(t)
	return d, nil
}
func (s *Service) ReadDocument(r *refinementv1.GetDocumentRequest, stream refinementv1.RefinementService_ReadDocumentServer) error {
	ctx := stream.Context()
	select {
	case s.reads <- struct{}{}:
		defer func() { <-s.reads }()
	case <-ctx.Done():
		return status.FromContextError(ctx.Err()).Err()
	}
	d, e := s.GetDocument(ctx, r)
	if e != nil {
		return e
	}
	key, e := canonical.Key(d.RefinementRunId, d.Sha256)
	if e != nil {
		return status.Error(codes.DataLoss, "invalid canonical identity")
	}
	data, e := canonical.Read(ctx, s.store, key, d.Sha256, d.SizeBytes)
	if e != nil {
		return status.Error(codes.DataLoss, "canonical integrity verification failed")
	}
	for len(data) > 0 {
		n := 64 << 10
		if n > len(data) {
			n = len(data)
		}
		if e = stream.Send(&refinementv1.DocumentChunk{Data: data[:n]}); e != nil {
			return e
		}
		data = data[n:]
	}
	return nil
}
