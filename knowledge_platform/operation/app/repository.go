package app

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/execution"

	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Keep this projection and scanOperation in the same order. Every Operation
// read shares it so lifecycle, dispatch and reconciliation see the same row.
const operationSelect = `SELECT operation_id,kind,execution_type,resource_type,resource_id,status,phase,attempt,max_attempts,
 COALESCE(error_code,''),COALESCE(error_message,''),retryable,resources,created_at,updated_at,started_at,completed_at,dispatch_protocol,dispatch_generation,COALESCE(dispatch_token::text,''),COALESCE(dispatch_owner,''),dispatch_lease_until,execution_started_at,COALESCE(execution_instance_id::text,''),gpu_wait_count,gpu_wait_until,progress_json,progress_updated_at,COALESCE(reconcile_note,'') FROM operations`

func (s *Service) get(ctx context.Context, id string) (*operationv1.Operation, error) {
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "operation_id is required")
	}
	row := s.pool.QueryRow(ctx, operationSelect+" WHERE operation_id=$1", id)
	op, err := scanOperation(row)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "operation not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "query operation: %v", err)
	}
	return op, nil
}

type rowScanner interface{ Scan(...any) error }

func scanOperation(row rowScanner) (*operationv1.Operation, error) {
	var id, kind, executionType, resourceType, resourceID, opStatus, phase string
	var attempt, maxAttempts int32
	var errorCode, errorMessage, reconcileNote string
	var retryable bool
	var resourcesJSON, progressJSON []byte
	var progressUpdatedAt *time.Time
	var createdAt, updatedAt time.Time
	var startedAt, completedAt *time.Time
	var protocol, generation, gpuWaitCount int32
	var token, owner, instance string
	var leaseUntil, executionStarted, gpuWaitUntil *time.Time
	if err := row.Scan(&id, &kind, &executionType, &resourceType, &resourceID, &opStatus, &phase, &attempt, &maxAttempts,
		&errorCode, &errorMessage, &retryable, &resourcesJSON, &createdAt, &updatedAt, &startedAt, &completedAt, &protocol, &generation, &token, &owner, &leaseUntil, &executionStarted, &instance, &gpuWaitCount, &gpuWaitUntil, &progressJSON, &progressUpdatedAt, &reconcileNote); err != nil {
		return nil, err
	}
	var resources execution.Resources
	if len(resourcesJSON) > 0 {
		if err := json.Unmarshal(resourcesJSON, &resources); err != nil {
			return nil, err
		}
	}
	op := &operationv1.Operation{
		GpuWaitCount: gpuWaitCount, ReconcileNote: reconcileNote,
		DispatchProtocol: protocol, DispatchGeneration: generation, DispatchToken: token, DispatchOwner: owner, ExecutionInstanceId: instance,
		OperationId: id, Kind: kind, ExecutionType: executionType, ResourceType: resourceType, ResourceId: resourceID,
		Status: opStatus, Phase: phase, Attempt: attempt, MaxAttempts: maxAttempts, Resources: resources.Proto(),
		ErrorCode: errorCode, ErrorMessage: errorMessage, Retryable: retryable,
		CreatedAt: timestamppb.New(createdAt), UpdatedAt: timestamppb.New(updatedAt),
	}
	if len(progressJSON) > 0 {
        op.Progress = &operationv1.TransferProgress{}
        if err := json.Unmarshal(progressJSON, op.Progress); err != nil { return nil, err }
    }
    if progressUpdatedAt != nil { op.ProgressUpdatedAt = timestamppb.New(*progressUpdatedAt) }
	if gpuWaitUntil != nil { op.GpuWaitUntil = timestamppb.New(*gpuWaitUntil) }
	if leaseUntil != nil {
		op.DispatchLeaseUntil = timestamppb.New(*leaseUntil)
	}
	if executionStarted != nil {
		op.ExecutionStartedAt = timestamppb.New(*executionStarted)
	}
	if startedAt != nil {
		op.StartedAt = timestamppb.New(*startedAt)
	}
	if completedAt != nil {
		op.CompletedAt = timestamppb.New(*completedAt)
	}
	return op, nil
}

func lockOperation(ctx context.Context, tx pgx.Tx, id string) (*operationv1.Operation, error) {
	op, err := scanOperation(tx.QueryRow(ctx, operationSelect+` WHERE operation_id=$1 FOR UPDATE`, id))
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "operation not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "lock operation: %v", err)
	}
	return op, nil
}

func commitOperation(ctx context.Context, tx pgx.Tx, id string) (*operationv1.Operation, error) {
	op, err := scanOperation(tx.QueryRow(ctx, operationSelect+` WHERE operation_id=$1`, id))
	if err != nil {
		return nil, status.Errorf(codes.Internal, "read operation: %v", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit operation: %v", err)
	}
	return op, nil
}
