package app

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	serving "model-platform/deployment/runtime"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/execution"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/submission"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (s *Service) submissions() submission.Worker {
	return submission.Worker{Pool: s.pool, Ensure: func(ctx context.Context, req *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
		return s.clients.Operation.EnsureOperation(ctx, req)
	}}
}
func (s *Service) RunSubmissions(ctx context.Context) { s.submissions().Run(ctx) }

// DeployModel commits the domain, reserved Operation ID and replay response
// before contacting Operation. Transport errors cannot fail a possibly live Job.
func (s *Service) createDeployment(ctx context.Context, req *deploymentv1.DeployModelRequest) (*deploymentv1.DeployModelResponse, error) {
	modelID, err := uuid.Parse(strings.TrimSpace(req.GetModelVersionId()))
	if err != nil || modelID == uuid.Nil {
		return nil, status.Error(codes.InvalidArgument, "model_version_id must be UUID")
	}
	modelVersionID := modelID.String()
	spec, err := serving.Normalize(req.GetServing())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	resources, err := execution.NormalizeResources(req.GetResources(), 0)
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid resources: %v", err)
	}
	if spec.Engine == "vllm" {
		if s.runtime == nil {
			return nil, status.Error(codes.FailedPrecondition, "vLLM serving is not configured on Deployment")
		}
		if resources.GPUCount != 1 || len(resources.GPUIds) != 1 {
			return nil, status.Error(codes.InvalidArgument, "vLLM serving requires resources.gpu_ids with exactly one GPU")
		}
		if resources.NodeName != "" && resources.NodeName != s.runtime.config.NodeName {
			return nil, status.Error(codes.InvalidArgument, "requested serving node does not match this Deployment service")
		}
	}
	encoded, err := json.Marshal(resources)
	if err != nil {
		return nil, err
	}
	fingerprint, err := idempotency.Fingerprint(struct {
		Version        int
		ModelVersionID string
		Resources      execution.Resources
	}{1, modelVersionID, resources}, nil)
	if err != nil {
		return nil, err
	}
	// Keep the exact v1 fingerprint for old demo requests and stored keys.
	if spec.Engine == "vllm" {
		fingerprint, err = idempotency.Fingerprint(struct {
			Version        int
			ModelVersionID string
			Resources      execution.Resources
			Serving        serving.Spec
		}{2, modelVersionID, resources, spec}, nil)
		if err != nil {
			return nil, err
		}
	}
	specJSON, _ := json.Marshal(spec)
	response := &deploymentv1.DeployModelResponse{}
	err = idempotency.Execute(ctx, s.pool, "deployment.create.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		if err := purge.RequireAvailable(ctx, tx, modelVersionID); err != nil {
			return err
		}
		lookupCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		mv, err := s.clients.Management.GetModelVersion(lookupCtx, &managementv1.GetModelVersionRequest{ModelVersionId: modelVersionID})
		if err != nil {
			if status.Code(err) == codes.NotFound {
				return status.Error(codes.NotFound, "source model version not found")
			}
			return status.Error(codes.Unavailable, "source model lookup unavailable")
		}
		if mv.GetState() != "ready" {
			return status.Error(codes.FailedPrecondition, "model version is not ready")
		}
		if mv.GetArtifactKind() == "adapter" && spec.Engine == "demo" {
			return status.Error(codes.FailedPrecondition, "adapter deployment requires serving.engine=vllm")
		}
		source := serving.Source{}
		kind, executionType, applyStatus := "model.deploy", "deployment.apply", ""
		jobResources := resources
		if spec.Engine == "vllm" {
			if mv.GetModelVersionId() != modelVersionID {
				return status.Error(codes.FailedPrecondition, "source model identity mismatch")
			}
			source, err = s.servingSource(lookupCtx, mv, spec.Profile)
			if err != nil {
				return err
			}
			kind, executionType, applyStatus = "model.serve", "deployment.serve", "created"
			// The one-shot coordinator is CPU-only; GPUs belong to the runtime.
			jobResources = execution.Resources{NodeName: resources.NodeName}
		}
		sourceJSON, _ := json.Marshal(source)
		storedEngine := spec.Engine
		// Older snapshot-only controllers select engine=vllm and must not
		// accidentally start an adapter request as an unadapted base model.
		if source.Adapter != nil {
			storedEngine = "vllm-lora"
		}
		if spec.Profile == serving.SuryaProfile {
			storedEngine = "vllm-surya"
		}
		id, operationID := uuid.NewString(), uuid.NewString()
		if _, err := tx.Exec(ctx, `INSERT INTO deployments(deployment_id,model_version_id,status,resources,engine,serving_spec,serving_source,apply_status) VALUES($1,$2,'created',$3::jsonb,$4,$5::jsonb,$6::jsonb,$7)`, id, modelVersionID, encoded, storedEngine, specJSON, sourceJSON, applyStatus); err != nil {
			return err
		}
		if err := submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: operationID, Kind: kind, ExecutionType: executionType, ResourceType: "deployment", ResourceId: id, MaxAttempts: 1, Resources: jobResources.Proto()}); err != nil {
			return err
		}
		response.DeploymentId, response.OperationId, response.Status = id, operationID, "pending"
		return nil
	})
	if err != nil {
		return nil, err
	}
	s.submissions().Try(ctx, response.GetOperationId())
	return response, nil
}

// Keep the legacy RPC restricted to demo so mixed-version callers cannot
// accidentally treat a placeholder deployment as a real inference endpoint.
func (s *Service) DeployModel(ctx context.Context, req *deploymentv1.DeployModelRequest) (*deploymentv1.DeployModelResponse, error) {
	spec, err := serving.Normalize(req.GetServing())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if spec.Engine != "demo" {
		return nil, status.Error(codes.InvalidArgument, "use CreateServingDeployment for vLLM")
	}
	return s.createDeployment(ctx, req)
}
func (s *Service) CreateServingDeployment(ctx context.Context, req *deploymentv1.DeployModelRequest) (*deploymentv1.DeployModelResponse, error) {
	spec, err := serving.Normalize(req.GetServing())
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if spec.Engine != "vllm" {
		return nil, status.Error(codes.InvalidArgument, "CreateServingDeployment requires vLLM")
	}
	// Normalize owns the supported-profile list; only Surya uses a separate RPC.
	if spec.Profile == serving.SuryaProfile {
		return nil, status.Error(codes.InvalidArgument, "use CreateOCRDeployment for the Surya profile")
	}
	return s.createDeployment(ctx, req)
}
