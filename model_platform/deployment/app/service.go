package app

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"model-platform/deployment/grpcclient"
	deploymentv1 "model-platform/gen/deployment/v1"
	"model-platform/internal/execution"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/submission"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Service struct {
	deploymentv1.UnimplementedDeploymentServiceServer
	pool    *pgxpool.Pool
	clients *grpcclient.Clients
	runtime *servingController
}

func New(pool *pgxpool.Pool, clients *grpcclient.Clients) *Service {
	return &Service{pool: pool, clients: clients}
}

func InitSchema(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, `
CREATE TABLE IF NOT EXISTS deployments(
 deployment_id UUID PRIMARY KEY,
 model_version_id UUID NOT NULL,
 status VARCHAR(32) NOT NULL,
 resources JSONB NOT NULL DEFAULT '{}'::jsonb,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 CONSTRAINT deployment_status_check CHECK(status IN ('created','applying','ready','failed'))
);
CREATE INDEX IF NOT EXISTS deployments_browse_idx ON deployments(created_at DESC,deployment_id DESC);
CREATE INDEX IF NOT EXISTS deployments_model_idx ON deployments(model_version_id,created_at DESC);
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS resources JSONB NOT NULL DEFAULT '{}'::jsonb;
`+purge.Schema+idempotency.Schema+submission.Schema+servingSchema)
	return err
}

func (s *Service) GetDeployment(ctx context.Context, req *deploymentv1.GetDeploymentRequest) (*deploymentv1.Deployment, error) {
	return s.get(ctx, strings.TrimSpace(req.GetDeploymentId()))
}

func (s *Service) CompleteDeployment(ctx context.Context, req *deploymentv1.CompleteDeploymentRequest) (*deploymentv1.Deployment, error) {
	id := strings.TrimSpace(req.GetDeploymentId())
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "deployment_id is required")
	}
	tag, err := s.pool.Exec(ctx, `UPDATE deployments SET status='ready',updated_at=NOW() WHERE deployment_id=$1 AND engine='demo' AND status IN ('created','applying','ready')`, id)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "complete deployment: %v", err)
	}
	if tag.RowsAffected() != 1 {
		return nil, status.Error(codes.FailedPrecondition, "only active demo deployments accept legacy completion")
	}
	return s.get(ctx, id)
}

func (s *Service) FailDeployment(ctx context.Context, req *deploymentv1.FailDeploymentRequest) (*deploymentv1.Deployment, error) {
	id := strings.TrimSpace(req.GetDeploymentId())
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "deployment_id is required")
	}
	tag, err := s.pool.Exec(ctx, `UPDATE deployments SET status='failed',updated_at=NOW() WHERE deployment_id=$1 AND engine='demo' AND status IN ('created','applying','failed')`, id)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "fail deployment: %v", err)
	}
	if tag.RowsAffected() != 1 {
		return nil, status.Error(codes.FailedPrecondition, "only unfinished demo deployments accept legacy failure")
	}
	return s.get(ctx, id)
}

func (s *Service) get(ctx context.Context, id string) (*deploymentv1.Deployment, error) {
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "deployment_id is required")
	}
	var modelVersionID, statusValue string
	var resourcesJSON []byte
	var created, updated time.Time
    var deleted *time.Time
	err := s.pool.QueryRow(ctx, `
SELECT model_version_id,status,resources,created_at,updated_at,deleted_at
FROM deployments WHERE deployment_id=$1
`, id).Scan(&modelVersionID, &statusValue, &resourcesJSON, &created, &updated, &deleted)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "deployment not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get deployment: %v", err)
	}
	var resources execution.Resources
	if len(resourcesJSON) > 0 {
		if err := json.Unmarshal(resourcesJSON, &resources); err != nil {
			return nil, status.Errorf(codes.Internal, "decode resources: %v", err)
		}
	}
	info, err := submission.Lookup(ctx, s.pool, "deployment", id)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	result := &deploymentv1.Deployment{OperationId: info.OperationID, SubmissionStatus: info.Status, SubmissionErrorCode: info.ErrorCode,
		DeploymentId:   id,
		ModelVersionId: modelVersionID,
		Status:         statusValue,
		CreatedAt:      timestamppb.New(created),
		UpdatedAt:      timestamppb.New(updated),
		Resources:      resources.Proto(),
	}
	if deleted != nil { result.DeletedAt = timestamppb.New(*deleted) }
	if err := s.servingView(ctx, id, result); err != nil {
		return nil, err
	}
	return result, nil
}
