package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/idempotency"
	"data-platform/internal/purge"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func sourcePurgeLock(ctx context.Context, tx pgx.Tx, id string) error {
	_, e := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))`, "source-purge:"+id)
	return e
}

// This fence shares a transaction lock with CreateRun, closing the lookup/insert race.
func (s *Service) BlockSourcePurge(ctx context.Context, r *refinementv1.GetDocumentRequest) (*refinementv1.PurgeFence, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	if e = sourcePurgeLock(ctx, tx, r.DocumentId); e != nil {
		return nil, idempotency.DBError(e)
	}
	var used bool
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM refinement_runs WHERE source_document_id=$1 AND purged_at IS NULL)`, r.DocumentId).Scan(&used); e != nil {
		return nil, idempotency.DBError(e)
	}
	if used {
		return nil, status.Error(codes.FailedPrecondition, "SOURCE_HAS_RESULTS: permanently delete its refinement results first")
	}
	if _, e = tx.Exec(ctx, `INSERT INTO source_purge_fences(document_id) VALUES($1) ON CONFLICT DO NOTHING`, r.DocumentId); e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return &refinementv1.PurgeFence{DocumentId: r.DocumentId}, nil
}
func (s *Service) PurgeRun(ctx context.Context, r *refinementv1.GetRunRequest) (*refinementv1.RefinementRun, error) {
	if e := validID(r.GetRefinementRunId()); e != nil {
		return nil, e
	}
	unlock, e := purge.Lock(ctx, s.pool, r.RefinementRunId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer unlock()
	run, e := s.GetRun(ctx, r)
	if e != nil {
		return nil, e
	}
	if run.PurgedAt != nil {
		return run, nil
	}
	if s.ops == nil {
		return nil, status.Error(codes.Unavailable, "operation service unavailable")
	}
	op, e := s.ops.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: run.OperationId})
	if e != nil {
		return nil, e
	}
	if !removableRun(op, run) {
		return nil, status.Error(codes.FailedPrecondition, "RESULT_ACTIVE: wait for the refinement to finish")
	}
	deleter, ok := s.store.(interface {
		PurgeRun(context.Context, string) error
	})
	if !ok {
		return nil, status.Error(codes.Unavailable, "permanent object deletion is unavailable")
	}
	// AcquireKnowledgeReferences uses the same purge-files lock. A reference
	// cannot be inserted between this check and the durable purging fence.
	var referenced bool
	if e = s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM knowledge_canonical_references k JOIN canonical_documents c ON c.document_id=k.canonical_id WHERE c.refinement_run_id=$1)`, run.RefinementRunId).Scan(&referenced); e != nil { return nil, idempotency.DBError(e) }
	if referenced { return nil, status.Error(codes.FailedPrecondition, "CANONICAL_IN_USE: delete the Knowledge index versions before permanent deletion") }
	// Fence completion and all downloads before the first destructive storage call.
	if _, e = s.pool.Exec(ctx, `UPDATE refinement_runs SET purge_state='purging' WHERE refinement_run_id=$1`, run.RefinementRunId); e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = deleter.PurgeRun(ctx, run.RefinementRunId); e != nil {
		return nil, status.Error(codes.Unavailable, "canonical deletion incomplete; retry the same item")
	}

	if _, e = s.pool.Exec(ctx, `UPDATE refinement_runs SET purge_state='purged',purged_at=COALESCE(purged_at,clock_timestamp()),deleted_at=COALESCE(deleted_at,clock_timestamp()) WHERE refinement_run_id=$1`, run.RefinementRunId); e != nil {
		return nil, idempotency.DBError(e)
	}
	return s.GetRun(ctx, r)
}
