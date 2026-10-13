package app

import (
	"context"
	"errors"
	"strings"

	operationv1 "knowledge-platform/gen/operation/v1"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Lifecycle reports from gated jobs must come from the authorized process.
func (s *Service) MarkRunning(ctx context.Context, req *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
	phase := valueOr(req.GetPhase(), "running")
	return s.transition(ctx, req.GetOperationId(), "running", phase, "", "", false, req.GetDispatchToken(), req.GetExecutionInstanceId())
}

func (s *Service) UpdatePhase(ctx context.Context, req *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
    if req.GetProgress() != nil { return s.updateTransferProgress(ctx, req) }
	if strings.TrimSpace(req.GetPhase()) == "" {
		return nil, status.Error(codes.InvalidArgument, "phase is required")
	}
	return s.transition(ctx, req.GetOperationId(), "phase", req.GetPhase(), "", "", false, req.GetDispatchToken(), req.GetExecutionInstanceId())
}

func (s *Service) SucceedOperation(ctx context.Context, req *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
	return s.transition(ctx, req.GetOperationId(), "succeeded", valueOr(req.GetPhase(), "completed"), "", "", false, req.GetDispatchToken(), req.GetExecutionInstanceId())
}

func (s *Service) FailOperation(ctx context.Context, req *operationv1.FailOperationRequest) (*operationv1.Operation, error) {
	return s.transition(ctx, req.GetOperationId(), "failed", valueOr(req.GetPhase(), "failed"), req.GetErrorCode(), req.GetErrorMessage(), req.GetRetryable(), req.GetDispatchToken(), req.GetExecutionInstanceId())
}

// Lock the operation through validation, mutation and response readback.
// Terminal duplicates preserve the first outcome, timestamps and error details.
func (s *Service) transition(ctx context.Context, id, target, phase, errorCode, errorMessage string, retryable bool, token, instance string) (*operationv1.Operation, error) {
	id = strings.TrimSpace(id)
	if _, err := uuid.Parse(id); err != nil {
		return nil, status.Error(codes.InvalidArgument, "valid operation_id is required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin operation transition: %v", err)
	}
	defer tx.Rollback(ctx)

	op, err := scanOperation(tx.QueryRow(ctx, operationSelect+" WHERE operation_id=$1 FOR UPDATE", id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "operation not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "lock operation: %v", err)
	}
	if op.GetDispatchProtocol() == 1 && (op.GetExecutionStartedAt() == nil || token != op.GetDispatchToken() || instance != op.GetExecutionInstanceId()) {
		return nil, status.Error(codes.FailedPrecondition, "execution report is not from the authorized process")
	}
	allowed, noop := transitionPolicy(op.GetStatus(), target)
	if !allowed {
		return nil, status.Errorf(codes.FailedPrecondition, "operation transition %s -> %s is not allowed", op.GetStatus(), target)
	}
	if noop {
		return op, nil
	}
	if target == "phase" {
		_, err = tx.Exec(ctx, `UPDATE operations SET phase=$2,updated_at=NOW() WHERE operation_id=$1`, id, strings.TrimSpace(phase))
	} else {
		_, err = tx.Exec(ctx, `UPDATE operations SET status=$2::varchar,phase=$3,error_code=$4,error_message=$5,retryable=$6,
  updated_at=NOW(),completed_at=CASE WHEN $2::varchar IN ('succeeded','failed') THEN NOW() ELSE completed_at END
  WHERE operation_id=$1`, id, target, phase, errorCode, errorMessage, retryable)
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "update operation: %v", err)
	}
	op, err = scanOperation(tx.QueryRow(ctx, operationSelect+" WHERE operation_id=$1", id))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "read updated operation: %v", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit operation transition: %v", err)
	}
	return op, nil
}

func valueOr(value, fallback string) string {
	if value = strings.TrimSpace(value); value != "" {
		return value
	}
	return fallback
}
