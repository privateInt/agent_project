package app

import (
	"context"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	serving "model-platform/deployment/runtime"
	deploymentv1 "model-platform/gen/deployment/v1"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/idempotency"
)

const servingSchema = `
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS engine TEXT NOT NULL DEFAULT 'demo';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS serving_spec JSONB NOT NULL DEFAULT '{}';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS serving_source JSONB NOT NULL DEFAULT '{}';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS desired_state TEXT NOT NULL DEFAULT 'running';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS apply_status TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_authorized BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_dispatch_token TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_instance_id TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_image_id TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_container_id TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_state TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_endpoint TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_error_message TEXT NOT NULL DEFAULT '';
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_launch_at TIMESTAMPTZ;
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS runtime_checked_at TIMESTAMPTZ;
ALTER TABLE deployments DROP CONSTRAINT IF EXISTS deployment_status_check;
ALTER TABLE deployments ADD CONSTRAINT deployment_status_check CHECK(status IN ('created','applying','ready','failed','unhealthy','stopping','stopped'));
CREATE INDEX IF NOT EXISTS deployments_lora_check_idx ON deployments(runtime_checked_at) WHERE engine='vllm-lora';
CREATE INDEX IF NOT EXISTS deployments_serving_check_idx ON deployments(runtime_checked_at) WHERE engine='vllm';
`

func (s *Service) ConfigureServing(config serving.Config, driver serving.Driver, fetcher serving.Fetcher) error {
	if err := config.Validate(); err != nil {
		return err
	}
	if driver == nil || fetcher == nil {
		return errors.New("serving driver and fetcher are required")
	}
	s.runtime = &servingController{service: s, config: config, driver: driver, fetcher: fetcher}
	return nil
}
func (s *Service) RunServing(ctx context.Context) {
	if s.runtime != nil {
		s.runtime.run(ctx)
	}
}

func deploymentID(raw string) (string, error) {
	id, err := uuid.Parse(strings.TrimSpace(raw))
	if err != nil || id == uuid.Nil {
		return "", status.Error(codes.InvalidArgument, "deployment_id must be a nonzero UUID")
	}
	return id.String(), nil
}

func validateServingGrant(req *deploymentv1.StartServingRequest, op *operationv1.Operation) error {
	if op.GetOperationId() != req.GetOperationId() || op.GetResourceType() != "deployment" || op.GetResourceId() != req.GetDeploymentId() || op.GetExecutionType() != "deployment.serve" || op.GetStatus() != "running" || op.GetDispatchProtocol() != 1 || op.GetExecutionStartedAt() == nil || req.GetDispatchToken() == "" || req.GetExecutionInstanceId() == "" || op.GetDispatchToken() != req.GetDispatchToken() || op.GetExecutionInstanceId() != req.GetExecutionInstanceId() {
		return status.Error(codes.FailedPrecondition, "serving requires the current approved deployment.serve process")
	}
	return nil
}

func (s *Service) StartServing(ctx context.Context, req *deploymentv1.StartServingRequest) (*deploymentv1.Deployment, error) {
	if s.runtime == nil {
		return nil, status.Error(codes.FailedPrecondition, "serving is not configured")
	}
	id, err := deploymentID(req.GetDeploymentId())
	if err != nil {
		return nil, err
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	defer idempotency.Rollback(tx)
	var engine, desired, operationID, token, instance string
	var authorized bool
	err = tx.QueryRow(ctx, `SELECT d.engine,d.desired_state,q.operation_id,d.runtime_authorized,d.runtime_dispatch_token,d.runtime_instance_id FROM deployments d JOIN operation_submissions q ON q.resource_id=d.deployment_id AND q.resource_type='deployment' WHERE d.deployment_id=$1 AND d.deleted_at IS NULL FOR UPDATE OF d`, id).Scan(&engine, &desired, &operationID, &authorized, &token, &instance)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "deployment submission not found")
	}
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	if (engine != "vllm" && engine != "vllm-lora" && engine != "vllm-surya") || operationID != req.GetOperationId() {
		return nil, status.Error(codes.FailedPrecondition, "serving submission identity mismatch")
	}
	if authorized {
		if token != req.GetDispatchToken() || instance != req.GetExecutionInstanceId() {
			return nil, status.Error(codes.FailedPrecondition, "serving was authorized by another process")
		}
	} else {
		if desired != "running" {
			return nil, status.Error(codes.FailedPrecondition, "deployment was stopped")
		}
		rpcCtx, rpcCancel := context.WithTimeout(ctx, 5*time.Second)
		op, callErr := s.clients.Operation.GetOperation(rpcCtx, &operationv1.GetOperationRequest{OperationId: operationID})
		rpcCancel()
		if callErr != nil {
			return nil, status.Error(codes.Unavailable, "execution grant lookup unavailable")
		}
		if err := validateServingGrant(req, op); err != nil {
			return nil, err
		}
		_, err = tx.Exec(ctx, `UPDATE deployments SET runtime_authorized=TRUE,runtime_dispatch_token=$2,runtime_instance_id=$3,status='applying',apply_status='applying',runtime_state='queued',updated_at=clock_timestamp() WHERE deployment_id=$1`, id, req.GetDispatchToken(), req.GetExecutionInstanceId())
		if err != nil {
			return nil, idempotency.DBError(err)
		}
	}
	if err := tx.Commit(ctx); err != nil {
		return nil, idempotency.DBError(err)
	}
	return s.get(ctx, id)
}

func (s *Service) StopDeployment(ctx context.Context, req *deploymentv1.StopDeploymentRequest) (*deploymentv1.Deployment, error) {
	id, err := deploymentID(req.GetDeploymentId())
	if err != nil {
		return nil, err
	}
	current, err := s.get(ctx, id)
	if err != nil {
		return nil, err
	}
	if current.GetServing().GetEngine() != "vllm" {
		return nil, status.Error(codes.FailedPrecondition, "demo has no serving runtime to stop")
	}
	_, err = s.pool.Exec(ctx, `UPDATE deployments SET desired_state='stopped',status='stopping',runtime_endpoint='',updated_at=clock_timestamp() WHERE deployment_id=$1 AND desired_state<>'stopped'`, id)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	// Best-effort stop is independent of another deployment's long download.
	// A timeout preserves desired_state; the durable controller keeps trying.
	var stopErr error
	if s.runtime != nil {
		stopCtx, stopCancel := context.WithTimeout(ctx, 8*time.Second)
		stopErr = s.runtime.process(stopCtx, id)
		stopCancel()
	}
	result, err := s.get(ctx, id)
	if err == nil && result.GetStatus() == "stopping" && s.runtime != nil && stopErr != nil {
		// Finish a slow graceful stop independently of another model download.
		// State remains durable if this process exits; no new runtime is started.
		go func() {
			retryCtx, retryCancel := context.WithTimeout(context.Background(), time.Minute)
			defer retryCancel()
			_ = s.runtime.process(retryCtx, id)
		}()
	}
	return result, err
}

func (s *Service) servingView(ctx context.Context, id string, r *deploymentv1.Deployment) error {
	var engine string
	var specJSON, sourceJSON []byte
	var checked *time.Time
	err := s.pool.QueryRow(ctx, `SELECT engine,serving_spec,serving_source,apply_status,runtime_state,runtime_endpoint,runtime_container_id,runtime_image_id,runtime_error_code,runtime_error_message,desired_state,runtime_checked_at FROM deployments WHERE deployment_id=$1`, id).Scan(&engine, &specJSON, &sourceJSON, &r.ApplyStatus, &r.RuntimeState, &r.Endpoint, &r.RuntimeContainerId, &r.RuntimeImageId, &r.RuntimeErrorCode, &r.RuntimeErrorMessage, &r.DesiredState, &checked)
	if err != nil {
		return idempotency.DBError(err)
	}
	var spec serving.Spec
	if err := json.Unmarshal(specJSON, &spec); err != nil {
		return status.Error(codes.Internal, "invalid stored serving spec")
	}
	spec.Engine = engine
	if engine == "vllm-lora" || engine == "vllm-surya" {
		spec.Engine = "vllm"
	}
	r.Serving = spec.Proto()
	if engine == "vllm" || engine == "vllm-lora" || engine == "vllm-surya" {
		var source serving.Source
		if err := json.Unmarshal(sourceJSON, &source); err != nil {
			return status.Error(codes.Internal, "invalid stored serving source")
		}
		if (engine == "vllm-lora") != (source.Adapter != nil) {
			return status.Error(codes.Internal, "stored serving engine/source mismatch")
		}
		plan := serving.Plan{DeploymentID: id, Source: source}
		r.ArtifactKind = "snapshot"
		r.BaseModelVersionId = source.ModelVersionID
		r.BaseServedModelName = plan.BaseModelName()
		if source.Adapter != nil {
			r.ArtifactKind = "adapter"
			r.AdapterModelVersionId = source.Adapter.ModelVersionID
		}
		r.ServedModelName = "deployment-" + id
		if r.RuntimeState == "ready" && r.DesiredState == "running" {
			r.InternalEndpoint = "http://model-serving-" + id + ":8000/v1"
		} else {
			r.Endpoint = ""
		}
	}
	if checked != nil {
		r.RuntimeCheckedAt = timestamppb.New(*checked)
	}
	return nil
}
