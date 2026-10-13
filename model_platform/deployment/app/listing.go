package app

import (
	"context"
	"encoding/json"
	"google.golang.org/protobuf/types/known/timestamppb"
	serving "model-platform/deployment/runtime"
	deploymentv1 "model-platform/gen/deployment/v1"
	"model-platform/internal/execution"
	"model-platform/internal/idempotency"
	"model-platform/internal/listing"
	"time"
)

func (s *Service) ListDeployments(ctx context.Context, req *deploymentv1.ListDeploymentsRequest) (*deploymentv1.ListDeploymentsResponse, error) {
	state, err := listing.Enum(req.GetStatus(), "status", "created", "applying", "ready", "failed", "unhealthy", "stopping", "stopped")
	if err != nil {
		return nil, err
	}
	model, err := listing.UUID(req.GetModelVersionId(), "model_version_id")
	if err != nil {
		return nil, err
	}
	runtimeState, err := listing.Enum(req.GetRuntimeState(), "runtime_state", "exited", "queued", "waiting_resources", "preparing", "unknown", "starting", "ready", "failed", "unhealthy", "stopping", "stopped")
	if err != nil {
		return nil, err
	}
	page, err := listing.New(req.GetLimit(), req.GetCursor(), "deployments", state, model, runtimeState)
	if err != nil {
		return nil, err
	}
	query := listing.Query{}
	query.Equal("d.status", state)
	query.Equal("d.model_version_id", model)
	query.Equal("d.runtime_state", runtimeState)
	suffix := query.Finish(page, "d.created_at", "d.deployment_id")
	rows, err := s.pool.Query(ctx, `SELECT d.deployment_id,d.model_version_id,d.status,d.resources,d.created_at,d.updated_at,
        d.engine,d.serving_spec,d.apply_status,d.runtime_state,d.runtime_endpoint,d.runtime_error_code,d.runtime_error_message,d.desired_state,
        COALESCE(s.operation_id::text,''),COALESCE(s.status,''),COALESCE(s.last_error_code,'')
        FROM (SELECT * FROM deployments WHERE deleted_at IS NULL) d LEFT JOIN operation_submissions s ON s.resource_type='deployment' AND s.resource_id=d.deployment_id`+suffix, query.Args...)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	defer rows.Close()
	out := &deploymentv1.ListDeploymentsResponse{}
	for rows.Next() {
		if len(out.Deployments) == page.Limit {
			last := out.Deployments[len(out.Deployments)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.DeploymentId)
			break
		}
		d := &deploymentv1.Deployment{}
		var created, updated time.Time
		var resourceJSON, specJSON []byte
		var engine string
		if err := rows.Scan(&d.DeploymentId, &d.ModelVersionId, &d.Status, &resourceJSON, &created, &updated, &engine, &specJSON,
			&d.ApplyStatus, &d.RuntimeState, &d.Endpoint, &d.RuntimeErrorCode, &d.RuntimeErrorMessage, &d.DesiredState,
			&d.OperationId, &d.SubmissionStatus, &d.SubmissionErrorCode); err != nil {
			return nil, idempotency.DBError(err)
		}
		var resources execution.Resources
		var spec serving.Spec
		if err := json.Unmarshal(resourceJSON, &resources); err != nil {
			return nil, idempotency.DBError(err)
		}
		if err := json.Unmarshal(specJSON, &spec); err != nil {
			return nil, idempotency.DBError(err)
		}
		spec.Engine = engine
		if engine == "vllm" || engine == "vllm-surya" {
			spec.Engine = "vllm"
			d.ArtifactKind = "snapshot"
		}
		if engine == "vllm-lora" {
			spec.Engine = "vllm"
			d.ArtifactKind = "adapter"
		}
		if engine == "vllm" || engine == "vllm-lora" || engine == "vllm-surya" {
			d.ServedModelName = "deployment-" + d.DeploymentId
		}
		if d.RuntimeState != "ready" || d.DesiredState != "running" {
			d.Endpoint = ""
		}
		d.Serving = spec.Proto()
		d.Resources = resources.Proto()
		d.CreatedAt = timestamppb.New(created)
		d.UpdatedAt = timestamppb.New(updated)
		out.Deployments = append(out.Deployments, d)
	}
	return out, idempotency.DBError(rows.Err())
}
