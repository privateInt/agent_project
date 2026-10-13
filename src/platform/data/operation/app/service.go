package app

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/execution"
	"data-platform/internal/idempotency"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type Service struct {
	operationv1.UnimplementedOperationServiceServer
	pool *pgxpool.Pool
}

func New(pool *pgxpool.Pool) *Service {
	return &Service{pool: pool}
}

// [FLOW-07] Common Operation boundary.
// Domain services create a durable lifecycle row here, but domain secrets are
// intentionally not part of this contract.
func (s *Service) CreateOperation(ctx context.Context, req *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	kind := strings.TrimSpace(req.GetKind())
	executionType := strings.TrimSpace(req.GetExecutionType())
	resourceType := strings.TrimSpace(req.GetResourceType())
	resourceID := strings.TrimSpace(req.GetResourceId())
	if kind == "" || executionType == "" || resourceType == "" || resourceID == "" {
		return nil, status.Error(codes.InvalidArgument, "kind, execution_type, resource_type and resource_id are required")
	}
	parsedResourceID, err := uuid.Parse(resourceID)
	if err != nil || parsedResourceID == uuid.Nil {
		return nil, status.Error(codes.InvalidArgument, "resource_id must be UUID")
	}
	resourceID = parsedResourceID.String()
	resources, err := execution.NormalizeResources(req.GetResources(), 0)
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid resources: %v", err)
	}
	resourcesJSON, err := json.Marshal(resources)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "encode resources: %v", err)
	}
	maxAttempts := req.GetMaxAttempts()
	if maxAttempts <= 0 {
		maxAttempts = 1
	}
	fingerprint, err := idempotency.Fingerprint(struct {
		Kind, ExecutionType, ResourceType, ResourceID string
		MaxAttempts int32
		Resources execution.Resources
	}{kind, executionType, resourceType, resourceID, maxAttempts, resources}, nil)
	if err != nil {
		return nil, err
	}
	operationID := strings.TrimSpace(req.GetOperationId())
	if operationID == "" {
		operationID = uuid.NewString()
	} else {
		parsed, parseErr := uuid.Parse(operationID)
		if parseErr != nil || parsed == uuid.Nil {
			return nil, status.Error(codes.InvalidArgument, "operation_id must be a nonzero UUID")
		}
		operationID = parsed.String()
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin operation creation: %v", err)
	}
	defer idempotency.Rollback(tx)
	tag, err := tx.Exec(ctx, `
INSERT INTO operations(operation_id, kind, execution_type, resource_type, resource_id, status, phase, max_attempts, resources, dispatch_protocol, creation_request_hash)
VALUES($1,$2,$3,$4,$5,'pending','queued',$6,$7::jsonb,1,$8)
ON CONFLICT(operation_id) DO NOTHING
`, operationID, kind, executionType, resourceType, resourceID, maxAttempts, resourcesJSON, fingerprint)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "insert operation: %v", err)
	}

	if tag.RowsAffected() == 0 {
		var previous string
		if err := tx.QueryRow(ctx, `SELECT COALESCE(creation_request_hash,'') FROM operations WHERE operation_id=$1`, operationID).Scan(&previous); err != nil {
			return nil, status.Errorf(codes.Internal, "read operation creation identity: %v", err)
		}
		if previous != fingerprint {
			return nil, status.Error(codes.AlreadyExists, "operation_id was already used with a different creation request")
		}
		if err := tx.Commit(ctx); err != nil {
			return nil, status.Errorf(codes.Internal, "confirm existing operation: %v", err)
		}
		return s.get(ctx, operationID)
	}

	// [FLOW-08] Build the domain-light execution command. It carries routing
	// identifiers plus generic compute placement, but never domain secrets.
	cmd := execution.Command{
		EventID:       uuid.NewString(),
		OperationID:   operationID,
		Kind:          kind,
		ExecutionType: executionType,
		ResourceType:  resourceType,
		ResourceID:    resourceID,
		Resources:     resources,
		OccurredAt:    time.Now().UTC(),
	}
	// Operation and its immutable command become durable together. Kafka is
	// deliberately outside the request transaction and is handled by Outbox.
	payload, err := json.Marshal(cmd)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "encode outbox command: %v", err)
	}
	if _, err = tx.Exec(ctx, `INSERT INTO operation_outbox(event_id,operation_id,payload) VALUES($1,$2,$3::jsonb)`, cmd.EventID, operationID, payload); err != nil {
		return nil, status.Errorf(codes.Internal, "insert outbox event: %v", err)
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit operation creation: %v", err)
	}

	return s.get(ctx, operationID)
}

func (s *Service) GetOperation(ctx context.Context, req *operationv1.GetOperationRequest) (*operationv1.Operation, error) {
	return s.get(ctx, strings.TrimSpace(req.GetOperationId()))
}

// EnsureOperation is the idempotent domain handoff. A caller must reserve the
// ID before this call; old CreateOperation callers may still omit it.
func (s *Service) EnsureOperation(ctx context.Context, req *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	if strings.TrimSpace(req.GetOperationId()) == "" {
		return nil, status.Error(codes.InvalidArgument, "EnsureOperation requires operation_id")
	}
	return s.CreateOperation(ctx, req)
}
