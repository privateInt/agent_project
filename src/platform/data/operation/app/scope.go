package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/execution"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Core lifecycle tests retain the reusable contract. The running Data service
// explicitly limits creation to the currently implemented domain profile.
type ScopedService struct{ *Service }

func validateScope(r *operationv1.CreateOperationRequest) error {
	p := r.GetResources()
	if r.GetKind() != r.GetExecutionType() || !execution.Supported(r.GetExecutionType(), r.GetResourceType()) || r.GetMaxAttempts() > 1 || r.GetMaxAttempts() < 0 || p.GetNodeName() != "" || p.GetGpuCount() != 0 || len(p.GetGpuIds()) != 0 {
		return status.Error(codes.InvalidArgument, "only supported local CPU collection/refinement executions are allowed")
	}
	return nil
}
func (s *ScopedService) CreateOperation(ctx context.Context, r *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	if err := validateScope(r); err != nil {
		return nil, err
	}
	return s.Service.CreateOperation(ctx, r)
}
func (s *ScopedService) EnsureOperation(ctx context.Context, r *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	if err := validateScope(r); err != nil {
		return nil, err
	}
	return s.Service.EnsureOperation(ctx, r)
}
