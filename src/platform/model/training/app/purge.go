package app

import (
	"context"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
)

func (s *Service) BlockModelPurge(ctx context.Context, r *trainingv1.ModelPurgeFence) (*trainingv1.ModelPurgeFence, error) {
	if e := purge.ValidID(r.GetModelVersionId()); e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer tx.Rollback(context.Background())
	if e = purge.VersionLock(ctx, tx, r.ModelVersionId); e != nil {
		return nil, e
	}
	rows, e := tx.Query(ctx, `SELECT t.training_run_id::text,t.status,COALESCE(q.operation_id::text,'') FROM training_runs t LEFT JOIN operation_submissions q ON q.resource_id=t.training_run_id AND q.resource_type='training_run' WHERE t.source_model_version_id=$1`, r.ModelVersionId)
	if e != nil {
		return nil, e
	}
	type item struct{ id, state, op string }
	var runs []item
	for rows.Next() {
		var v item
		if e = rows.Scan(&v.id, &v.state, &v.op); e != nil {
			rows.Close()
			return nil, e
		}
		runs = append(runs, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	var activeRecovery bool
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM training_output_recoveries o JOIN training_runs t USING(training_run_id) WHERE t.source_model_version_id=$1 AND o.status IN ('queued','running'))`, r.ModelVersionId).Scan(&activeRecovery); e != nil {
		return nil, e
	}
	if activeRecovery {
		return nil, status.Error(codes.FailedPrecondition, "MODEL_TRAINING: output recovery is active")
	}
	for _, v := range runs {
		if v.state != "succeeded" && v.state != "failed" && v.state != "cancelled" {
			return nil, status.Error(codes.FailedPrecondition, "MODEL_TRAINING: wait for training to finish")
		}
		if v.op == "" || s.clients == nil || s.clients.Operation == nil {
			return nil, status.Error(codes.Unavailable, "training operation check unavailable")
		}
		op, e := s.clients.Operation.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: v.op})
		if e != nil {
			return nil, e
		}
		if op.GetOperationId() != v.op || op.GetResourceId() != v.id || op.GetResourceType() != "training_run" || (op.GetStatus() != "succeeded" && op.GetStatus() != "failed" && op.GetStatus() != "cancelled") {
			return nil, status.Error(codes.FailedPrecondition, "MODEL_TRAINING: training operation is not terminal")
		}
		if s.outputControl == nil {
			return nil, status.Error(codes.Unavailable, "producer stop check unavailable")
		}
		var stopErr error
		if observer, ok := s.outputControl.(interface {
			PurgeStopped(context.Context, *operationv1.Operation) error
		}); ok {
			stopErr = observer.PurgeStopped(ctx, op)
		} else {
			stopErr = s.outputControl.OutputStopped(ctx, op)
		}
		if stopErr != nil {
			return nil, status.Error(codes.FailedPrecondition, "MODEL_TRAINING: training container stop is unconfirmed")
		}

	}
	if e = purge.Fence(ctx, tx, r.ModelVersionId); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return &trainingv1.ModelPurgeFence{ModelVersionId: r.ModelVersionId}, nil
}
