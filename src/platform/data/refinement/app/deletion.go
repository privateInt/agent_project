package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/idempotency"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"time"
)

func removableRun(op *operationv1.Operation, run *refinementv1.RefinementRun) bool {
	if op == nil || run == nil || op.GetOperationId() != run.GetOperationId() ||
		op.GetResourceId() != run.GetRefinementRunId() || op.GetResourceType() != "refinement_run" ||
		op.GetExecutionType() != "refinement.parse" {
		return false
	}
	switch op.GetStatus() {
	case "succeeded", "failed", "cancelled":
		return true
	}
	return false
}

// DeleteRun hides only a terminal run from the result list. It does not cancel
// work or delete canonical/source bytes. Terminal Operation states are final.
func (s *Service) DeleteRun(ctx context.Context, r *refinementv1.GetRunRequest) (*refinementv1.RefinementRun, error) {
	if e := validID(r.GetRefinementRunId()); e != nil {
		return nil, e
	}
	run, e := scanRun(s.pool.QueryRow(ctx, runSelect+` WHERE refinement_run_id=$1`, r.RefinementRunId))
	if e != nil {
		return nil, readError(e)
	}
	if run.DeletedAt != nil {
		return run, nil
	} // Retry survives an Operation outage.
	if s.ops == nil {
		return nil, status.Error(codes.Unavailable, "operation service unavailable")
	}
	c, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	op, e := s.ops.GetOperation(c, &operationv1.GetOperationRequest{OperationId: run.OperationId})
	if status.Code(e) == codes.NotFound {
		return nil, status.Error(codes.FailedPrecondition, "refinement operation is not terminal")
	}
	if e != nil {
		return nil, e
	}
	if !removableRun(op, run) {
		return nil, status.Error(codes.FailedPrecondition, "refinement operation is not terminal or identity differs")
	}
	_, e = s.pool.Exec(ctx, `UPDATE refinement_runs SET deleted_at=COALESCE(deleted_at,clock_timestamp()) WHERE refinement_run_id=$1`, r.RefinementRunId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	return s.GetRun(ctx, r)
}
