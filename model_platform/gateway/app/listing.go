package app

import (
	"context"
	"github.com/gofiber/fiber/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/listing"
	"net/url"
	"strconv"
	"time"
)

func listQuery(c fiber.Ctx, filters ...string) (url.Values, int32, error) {
	values, err := url.ParseQuery(string(c.Request().URI().QueryString()))
	if err != nil {
		return nil, 0, status.Error(codes.InvalidArgument, "invalid query string")
	}
	allowed := map[string]bool{"limit": true, "cursor": true}
	for _, key := range filters {
		allowed[key] = true
	}
	for key, entries := range values {
		if !allowed[key] || len(entries) != 1 {
			return nil, 0, status.Errorf(codes.InvalidArgument, "unknown or repeated query parameter: %s", key)
		}
	}
	raw := "20"
	if value, ok := values["limit"]; ok {
		raw = value[0]
	}
	limit, err := strconv.ParseInt(raw, 10, 32)
	if err != nil || limit < 1 || limit > 100 {
		return nil, 0, status.Error(codes.InvalidArgument, "limit must be 1..100")
	}
	if len(values.Get("cursor")) > 2048 {
		return nil, 0, status.Error(codes.InvalidArgument, "invalid cursor")
	}
	return values, int32(limit), nil
}

func writeList[T any](c fiber.Ctx, values []T, cursor string, mapper func(T) fiber.Map) error {
	items := make([]fiber.Map, 0, len(values))
	for _, value := range values {
		items = append(items, mapper(value))
	}
	return c.JSON(fiber.Map{"items": items, "next_cursor": cursor})
}

func (a *API) listModels(c fiber.Ctx) error {
	q, limit, err := listQuery(c, "q", "model_type", "include_deleted")
	if err != nil {
		return grpcError(c, err)
	}
	if flag := q.Get("include_deleted"); flag != "" && flag != "true" && flag != "false" {
		return grpcError(c, status.Error(codes.InvalidArgument, "include_deleted must be true or false"))
	}
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	out, err := a.clients.Management.ListModels(ctx, &managementv1.ListModelsRequest{Limit: limit, Cursor: q.Get("cursor"), Q: q.Get("q"), ModelType: q.Get("model_type"), IncludeDeleted: q.Get("include_deleted") == "true"})
	if err != nil {
		return grpcError(c, err)
	}
	return writeList(c, out.GetModels(), out.GetNextCursor(), modelListMap)
}
func modelListMap(r *managementv1.Model) fiber.Map {
	return fiber.Map{"model_id": r.GetModelId(), "name": r.GetName(), "model_type": r.GetModelType(), "description": r.GetDescription(), "created_at": optionalTime(r.GetCreatedAt()), "updated_at": optionalTime(r.GetUpdatedAt()), "purge_state": r.GetPurgeState(), "purged_at": optionalTime(r.GetPurgedAt()), "deleted_at": optionalTime(r.GetDeletedAt())}
}

func (a *API) listModelVersions(c fiber.Ctx) error {
	q, limit, err := listQuery(c, "q", "model_id", "state", "artifact_kind", "source_type")
	if err != nil {
		return grpcError(c, err)
	}
	modelID := q.Get("model_id")
	if c.Params("id") != "" {
		if _, ok := q["model_id"]; ok {
			return grpcError(c, status.Error(codes.InvalidArgument, "model_id is already specified in the path"))
		}
		modelID, err = listing.UUID(c.Params("id"), "model_id")
		if err != nil {
			return grpcError(c, err)
		}
		if modelID == "" {
			return grpcError(c, status.Error(codes.InvalidArgument, "model_id is required"))
		}
	}
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	out, err := a.clients.Management.ListModelVersions(ctx, &managementv1.ListModelVersionsRequest{Limit: limit, Cursor: q.Get("cursor"), Q: q.Get("q"), ModelId: modelID, State: q.Get("state"), ArtifactKind: q.Get("artifact_kind"), SourceType: q.Get("source_type")})
	if err != nil {
		return grpcError(c, err)
	}
	return writeList(c, out.GetVersions(), out.GetNextCursor(), modelVersionListMap)
}
func modelVersionListMap(r *managementv1.ModelVersion) fiber.Map {
	return fiber.Map{"model_version_id": r.GetModelVersionId(), "model_id": r.GetModelId(), "model_name": r.GetModelName(), "model_type": r.GetModelType(),
		"version": r.GetVersion(), "state": r.GetState(), "source_type": r.GetSourceType(), "artifact_kind": r.GetArtifactKind(), "repo_id": r.GetRepoId(),
		"training_run_id": r.GetTrainingRunId(), "source_model_version_id": r.GetSourceModelVersionId(),
		"operation_id": r.GetOperationId(), "submission_status": r.GetSubmissionStatus(), "submission_error_code": r.GetSubmissionErrorCode(),
		"created_at": optionalTime(r.GetCreatedAt()), "updated_at": optionalTime(r.GetUpdatedAt())}
}

func (a *API) listTrainingRuns(c fiber.Ctx) error {
	q, limit, err := listQuery(c, "status", "source_model_version_id", "dataset_version_id")
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	out, err := a.clients.Training.ListTrainingRuns(ctx, &trainingv1.ListTrainingRunsRequest{Limit: limit, Cursor: q.Get("cursor"), Status: q.Get("status"), SourceModelVersionId: q.Get("source_model_version_id"), DatasetVersionId: q.Get("dataset_version_id")})
	if err != nil {
		return grpcError(c, err)
	}
	return writeList(c, out.GetRuns(), out.GetNextCursor(), trainingListMap)
}
func trainingListMap(r *trainingv1.TrainingRun) fiber.Map {
	return fiber.Map{"training_run_id": r.GetTrainingRunId(), "source_model_version_id": r.GetSourceModelVersionId(), "status": r.GetStatus(),
		"dataset": datasetMap(r.GetDataset()), "training": trainingSpecMap(r.GetTraining()), "resources": resourcesMap(r.GetResources()),
		"output_model_version_id": r.GetOutputModelVersionId(), "mlflow_experiment_id": r.GetMlflowExperimentId(), "mlflow_run_id": r.GetMlflowRunId(), "mlflow_run_url": r.GetMlflowRunUrl(),
		"cancel_requested_at": optionalTime(r.GetCancelRequestedAt()), "cancelled_at": optionalTime(r.GetCancelledAt()), "cancellation_error_code": r.GetCancellationErrorCode(),
		"evaluation": evaluationSpecMap(r.GetEvaluation()), "resume": resumeMap(r.GetResume()), "output_recovery_status": r.GetOutputRecoveryStatus(), "recovered_model_version_id": r.GetRecoveredModelVersionId(),
		"operation_id": r.GetOperationId(), "submission_status": r.GetSubmissionStatus(), "submission_error_code": r.GetSubmissionErrorCode(),
		"created_at": optionalTime(r.GetCreatedAt()), "updated_at": optionalTime(r.GetUpdatedAt())}
}

func (a *API) listDeployments(c fiber.Ctx) error {
	q, limit, err := listQuery(c, "status", "model_version_id", "runtime_state")
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	out, err := a.clients.Deployment.ListDeployments(ctx, &deploymentv1.ListDeploymentsRequest{Limit: limit, Cursor: q.Get("cursor"), Status: q.Get("status"), ModelVersionId: q.Get("model_version_id"), RuntimeState: q.Get("runtime_state")})
	if err != nil {
		return grpcError(c, err)
	}
	return writeList(c, out.GetDeployments(), out.GetNextCursor(), deploymentListMap)
}
func deploymentListMap(r *deploymentv1.Deployment) fiber.Map {
	return fiber.Map{"deployment_id": r.GetDeploymentId(), "model_version_id": r.GetModelVersionId(), "status": r.GetStatus(),
		"apply_status": r.GetApplyStatus(), "runtime_state": r.GetRuntimeState(), "desired_state": r.GetDesiredState(),
		"runtime_error_code": r.GetRuntimeErrorCode(), "runtime_error_message": r.GetRuntimeErrorMessage(), "endpoint": r.GetEndpoint(),
		"served_model_name": r.GetServedModelName(), "artifact_kind": r.GetArtifactKind(), "serving": fiber.Map{"engine": r.GetServing().GetEngine(), "profile": r.GetServing().GetProfile(), "max_model_len": r.GetServing().GetMaxModelLen(), "gpu_memory_utilization": r.GetServing().GetGpuMemoryUtilization()}, "resources": resourcesMap(r.GetResources()),
		"operation_id": r.GetOperationId(), "submission_status": r.GetSubmissionStatus(), "submission_error_code": r.GetSubmissionErrorCode(),
		"created_at": optionalTime(r.GetCreatedAt()), "updated_at": optionalTime(r.GetUpdatedAt())}
}

func (a *API) listOperations(c fiber.Ctx) error {
	q, limit, err := listQuery(c, "status", "resource_type", "resource_id", "kind", "phase")
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	out, err := a.clients.Operation.ListOperations(ctx, &operationv1.ListOperationsRequest{Limit: limit, Cursor: q.Get("cursor"), Status: q.Get("status"), ResourceType: q.Get("resource_type"), ResourceId: q.Get("resource_id"), Kind: q.Get("kind"), Phase: q.Get("phase")})
	if err != nil {
		return grpcError(c, err)
	}
	return writeList(c, out.GetOperations(), out.GetNextCursor(), operationListMap)
}
func operationListMap(r *operationv1.Operation) fiber.Map {
	result := operationMap(r)
	result["created_at"] = optionalTime(r.GetCreatedAt())
	result["updated_at"] = optionalTime(r.GetUpdatedAt())
	result["started_at"] = optionalTime(r.GetStartedAt())
	result["completed_at"] = optionalTime(r.GetCompletedAt())
	return result
}
