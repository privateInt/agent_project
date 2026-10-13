package app

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"model-platform/gateway/grpcclient"
	commonv1 "model-platform/gen/common/v1"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"

	"github.com/gofiber/fiber/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type API struct {
	clients  *grpcclient.Clients
	chatHTTP *http.Client
}

func New(clients *grpcclient.Clients) *API {
	return &API{clients: clients, chatHTTP: newChatHTTPClient()}
}

func (a *API) Register(app *fiber.App) {
	app.Get("/api/models", a.listModels)
	app.Get("/api/model-versions", a.listModelVersions)
	app.Get("/api/models/:id/versions", a.listModelVersions)
	app.Get("/api/training-runs", a.listTrainingRuns)
	app.Get("/api/deployments", a.listDeployments)
	app.Get("/api/operations", a.listOperations)
	app.Post("/api/datasets", a.createDataset)
	app.Get("/api/datasets", a.listDatasets)
	app.Get("/api/datasets/:id", a.getDataset)
	app.Post("/api/datasets/:id/versions", a.uploadDatasetVersion)
	app.Get("/api/datasets/:id/versions", a.listDatasetVersions)
	app.Get("/api/dataset-versions/:id", a.getDatasetVersion)
	app.Get("/health", func(c fiber.Ctx) error { return c.JSON(fiber.Map{"service": "model-gateway", "status": "ok"}) })
	app.Post("/api/models", a.createModel)
	app.Get("/api/models/:id", a.getModel)
	app.Delete("/api/models/:id", a.deleteModel)
    app.Patch("/api/models/:id", a.renameModel)
    app.Patch("/api/model-versions/:id", a.renameModelVersion)
    app.Delete("/api/deployments/:id", a.deleteDeployment)
	app.Post("/api/models/:id/purge", a.purgeModel)
	app.Post("/api/models/:id/imports/huggingface", a.importHuggingFace)
	app.Get("/api/model-versions/:id", a.getModelVersion)
	app.Get("/api/operations/:id", a.getOperation)
	app.Post("/api/training-runs", a.startTraining)
	app.Get("/api/training-runs/compare", a.compareTrainingRuns)
	app.Get("/api/training-runs/:id/evaluation", a.getTrainingEvaluation)
	app.Get("/api/training-runs/:id", a.getTrainingRun)
	app.Get("/api/training-runs/:id/checkpoints", a.listTrainingCheckpoints)
	app.Post("/api/training-runs/:id/resume", a.resumeTraining)
	app.Post("/api/training-runs/:id/cancel", a.cancelTraining)
	app.Get("/api/training-runs/:id/logs", a.getTrainingLogs)
	app.Get("/api/training-runs/:id/output", a.getTrainingOutput)
	app.Post("/api/training-runs/:id/output/retry", a.retryTrainingOutput)
	app.Post("/api/training-runs/:id/output/release", a.releaseTrainingOutput)
	app.Post("/api/deployments", a.deployModel)
	app.Get("/api/deployments/:id", a.getDeployment)
	app.Post("/api/deployments/:id/stop", a.stopDeployment)
	app.Post("/api/deployments/:id/chat", a.chatDeployment)
    app.Get("/api/deployments/:id/reranker-profile", a.rerankerProfile)
    app.Post("/api/deployments/:id/rerank", a.rerankDeployment)
    app.Get("/api/deployments/:id/embedding-profile", a.embeddingProfile)
    app.Post("/api/deployments/:id/embeddings", a.embedDeployment)
}

type createModelBody struct {
	Name        string `json:"name"`
	ModelType   string `json:"model_type"`
	Description string `json:"description"`
}

func (a *API) createModel(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 25*time.Second)
	defer cancel()
	var body createModelBody
	if err := c.Bind().Body(&body); err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid body"})
	}
	res, err := a.clients.Management.CreateModel(ctx, &managementv1.CreateModelRequest{IdempotencyKey: key, Name: body.Name, ModelType: body.ModelType, Description: body.Description})
	if err != nil {
		return grpcError(c, err)
	}
	return c.Status(201).JSON(fiber.Map{"model_id": res.GetModelId(), "name": res.GetName(), "model_type": res.GetModelType(), "description": res.GetDescription()})
}
func (a *API) getModel(c fiber.Ctx) error {
	res, err := a.clients.Management.GetModel(c.Context(), &managementv1.GetModelRequest{ModelId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.JSON(modelListMap(res))
}

type importBody struct {
	Version  string `json:"version"`
	RepoID   string `json:"repo_id"`
	Revision string `json:"revision"`
	HFToken  string `json:"hf_token"`
}

// [FLOW-02] External REST boundary for Hugging Face import.
// hf_token is accepted from the caller but never returned or logged here.
func (a *API) importHuggingFace(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 25*time.Second)
	defer cancel()
	var body importBody
	if err := c.Bind().Body(&body); err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid body"})
	}
	// [FLOW-03] REST -> Management gRPC. The credential is still inside the
	// trusted control path; Management will encrypt it before async dispatch.
	res, err := a.clients.Management.ImportHuggingFace(ctx, &managementv1.ImportHuggingFaceRequest{IdempotencyKey: key,
		ModelId: c.Params("id"), Version: body.Version, RepoId: body.RepoID, Revision: body.Revision, HfToken: body.HFToken,
	})
	if err != nil {
		return grpcError(c, err)
	}
	return c.Status(202).JSON(fiber.Map{"model_version_id": res.GetModelVersionId(), "operation_id": res.GetOperationId(), "status": res.GetStatus()})
}
func (a *API) getModelVersion(c fiber.Ctx) error {
	res, err := a.clients.Management.GetModelVersion(c.Context(), &managementv1.GetModelVersionRequest{ModelVersionId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.JSON(fiber.Map{"model_version_id": res.GetModelVersionId(), "model_id": res.GetModelId(), "version": res.GetVersion(), "state": res.GetState(), "repo_id": res.GetRepoId(), "requested_revision": res.GetRequestedRevision(), "resolved_revision": res.GetResolvedRevision(), "artifact_uri": res.GetArtifactUri(), "artifact_sha256": res.GetArtifactSha256(), "artifact_size_bytes": res.GetArtifactSizeBytes(), "artifact_kind": res.GetArtifactKind(),
		"source_type": res.GetSourceType(), "training_run_id": res.GetTrainingRunId(), "source_model_version_id": res.GetSourceModelVersionId(),
		"base_model_artifact_uri": res.GetBaseModelArtifactUri(), "artifact_manifest_sha256": res.GetArtifactManifestSha256(),
		"artifact_file_count": res.GetArtifactFileCount(), "operation_id": res.GetOperationId(), "submission_status": res.GetSubmissionStatus(), "submission_error_code": res.GetSubmissionErrorCode()})
}
func (a *API) getOperation(c fiber.Ctx) error {
	res, err := a.clients.Operation.GetOperation(c.Context(), &operationv1.GetOperationRequest{OperationId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.JSON(operationMap(res))
}

type resourcesBody struct {
	NodeName string  `json:"node_name"`
	GPUCount int32   `json:"gpu_count"`
	GPUIds   []int32 `json:"gpu_ids"`
}

func (r resourcesBody) proto() *commonv1.ExecutionResources {
	return &commonv1.ExecutionResources{NodeName: r.NodeName, GpuCount: r.GPUCount, GpuIds: append([]int32(nil), r.GPUIds...)}
}

func resourcesMap(r *commonv1.ExecutionResources) fiber.Map {
	if r == nil {
		return fiber.Map{"node_name": "", "gpu_count": 0, "gpu_ids": []int32{}}
	}
	return fiber.Map{"node_name": r.GetNodeName(), "gpu_count": r.GetGpuCount(), "gpu_ids": r.GetGpuIds()}
}

type datasetBody struct {
	SourceType       string `json:"source_type"`
	SourceURI        string `json:"source_uri"`
	Format           string `json:"format"`
	DatasetVersionID string `json:"dataset_version_id"`
	SHA256           string `json:"sha256"`
	SizeBytes        int64  `json:"size_bytes"`
	RecordCount      int64  `json:"record_count"`
}

func (d datasetBody) proto() *trainingv1.DatasetSpec {
	return &trainingv1.DatasetSpec{SourceType: d.SourceType, SourceUri: d.SourceURI, Format: d.Format, DatasetVersionId: d.DatasetVersionID, Sha256: d.SHA256, SizeBytes: d.SizeBytes, RecordCount: d.RecordCount}
}

type loraBody struct {
	R       int32   `json:"r"`
	Alpha   int32   `json:"alpha"`
	Dropout float64 `json:"dropout"`
}

type trainingSpecBody struct {
	CheckpointSteps           int32    `json:"checkpoint_steps"`
	Method                    string   `json:"method"`
	SequenceLength            int32    `json:"sequence_length"`
	MaxSteps                  int32    `json:"max_steps"`
	MicroBatchSize            int32    `json:"micro_batch_size"`
	GradientAccumulationSteps int32    `json:"gradient_accumulation_steps"`
	LearningRate              float64  `json:"learning_rate"`
	Lora                      loraBody `json:"lora"`
	SamplePacking             bool     `json:"sample_packing"`
	Strategy                  string   `json:"strategy"`
}

func (t trainingSpecBody) proto() *trainingv1.TrainingSpec {
	return &trainingv1.TrainingSpec{CheckpointSteps: t.CheckpointSteps,
		Strategy:                  t.Strategy,
		Method:                    t.Method,
		SequenceLength:            t.SequenceLength,
		MaxSteps:                  t.MaxSteps,
		MicroBatchSize:            t.MicroBatchSize,
		GradientAccumulationSteps: t.GradientAccumulationSteps,
		LearningRate:              t.LearningRate,
		Lora:                      &trainingv1.LoraSpec{R: t.Lora.R, Alpha: t.Lora.Alpha, Dropout: t.Lora.Dropout},
		SamplePacking:             t.SamplePacking,
	}
}

type trainingBody struct {
	SourceModelVersionID string           `json:"source_model_version_id"`
	Dataset              datasetBody      `json:"dataset"`
	Training             trainingSpecBody `json:"training"`
	Resources            resourcesBody    `json:"resources"`
}

// [TRAIN-FLOW-01] External REST boundary for a training request.
// Gateway only translates REST -> Training gRPC; it does not execute training.
func (a *API) startTraining(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 25*time.Second)
	defer cancel()
	var body trainingBody
	if err := c.Bind().Body(&body); err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid body"})
	}
	evaluation, err := parseEvaluationBody(c.Body())
	if err != nil {
		return c.Status(400).JSON(fiber.Map{"error": err.Error()})
	}
	res, err := a.clients.Training.StartTraining(ctx, &trainingv1.StartTrainingRequest{IdempotencyKey: key, Evaluation: evaluation,
		SourceModelVersionId: body.SourceModelVersionID,
		Dataset:              body.Dataset.proto(),
		Training:             body.Training.proto(),
		Resources:            body.Resources.proto(),
	})
	if err != nil {
		return grpcError(c, err)
	}
	return c.Status(202).JSON(fiber.Map{"training_run_id": res.GetTrainingRunId(), "operation_id": res.GetOperationId(), "status": res.GetStatus()})
}
func (a *API) getTrainingRun(c fiber.Ctx) error {
	res, err := a.clients.Training.GetTrainingRun(c.Context(), &trainingv1.GetTrainingRunRequest{TrainingRunId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.JSON(trainingDetailMap(res))
}
func trainingDetailMap(res *trainingv1.TrainingRun) fiber.Map {
	return fiber.Map{
		"training_run_id":     res.GetTrainingRunId(),
		"cancel_requested_at": optionalTime(res.GetCancelRequestedAt()), "cancelled_at": optionalTime(res.GetCancelledAt()), "cancellation_error_code": res.GetCancellationErrorCode(),
		"evaluation": evaluationSpecMap(res.GetEvaluation()), "resume": resumeMap(res.GetResume()), "output_recovery_status": res.GetOutputRecoveryStatus(), "recovered_model_version_id": res.GetRecoveredModelVersionId(),
		"operation_id": res.GetOperationId(), "submission_status": res.GetSubmissionStatus(), "submission_error_code": res.GetSubmissionErrorCode(),
		"source_model_version_id": res.GetSourceModelVersionId(),
		"status":                  res.GetStatus(),
		"dataset":                 datasetMap(res.GetDataset()),
		"training":                trainingSpecMap(res.GetTraining()),
		"resources":               resourcesMap(res.GetResources()),
		"result_json":             res.GetResultJson(),
		"result":                  parseResultJSON(res.GetResultJson()),
		"mlflow_experiment_id":    res.GetMlflowExperimentId(),
		"mlflow_run_id":           res.GetMlflowRunId(),
		"mlflow_run_url":          res.GetMlflowRunUrl(),
		"output_model_version_id": res.GetOutputModelVersionId(),
	}
}

func datasetMap(d *trainingv1.DatasetSpec) fiber.Map {
	if d == nil {
		return fiber.Map{"source_type": "", "source_uri": "", "format": ""}
	}
	out := fiber.Map{"source_type": d.GetSourceType(), "source_uri": d.GetSourceUri(), "format": d.GetFormat()}
	if d.GetDatasetVersionId() != "" {
		out["dataset_version_id"], out["sha256"], out["size_bytes"], out["record_count"] = d.GetDatasetVersionId(), d.GetSha256(), d.GetSizeBytes(), d.GetRecordCount()
	}
	return out
}

func trainingSpecMap(t *trainingv1.TrainingSpec) fiber.Map {
	if t == nil {
		return fiber.Map{}
	}
	lora := t.GetLora()
	loraMap := fiber.Map{}
	if lora != nil {
		loraMap = fiber.Map{"r": lora.GetR(), "alpha": lora.GetAlpha(), "dropout": lora.GetDropout()}
	}
	return fiber.Map{
		"method":                      t.GetMethod(),
		"sequence_length":             t.GetSequenceLength(),
		"max_steps":                   t.GetMaxSteps(),
		"micro_batch_size":            t.GetMicroBatchSize(),
		"gradient_accumulation_steps": t.GetGradientAccumulationSteps(),
		"learning_rate":               t.GetLearningRate(),
		"lora":                        loraMap,
		"sample_packing":              t.GetSamplePacking(),
		"checkpoint_steps":            t.GetCheckpointSteps(),
		"strategy":                    t.GetStrategy(),
	}
}

type servingBody struct {
	Profile              string  `json:"profile"`
	Engine               string  `json:"engine"`
	MaxModelLen          int32   `json:"max_model_len"`
	GPUMemoryUtilization float64 `json:"gpu_memory_utilization"`
}

func (s *servingBody) proto() *deploymentv1.ServingSpec {
	if s == nil {
		return nil
	}
	return &deploymentv1.ServingSpec{Engine: s.Engine, Profile: s.Profile, MaxModelLen: s.MaxModelLen, GpuMemoryUtilization: s.GPUMemoryUtilization}
}

type deploymentBody struct {
	Serving        *servingBody  `json:"serving"`
	ModelVersionID string        `json:"model_version_id"`
	Resources      resourcesBody `json:"resources"`
}

// [DEPLOY-FLOW-01] External REST boundary for a deployment request.
// Gateway only translates REST -> Deployment gRPC; serving is not started here.
func (a *API) deployModel(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil {
		return grpcError(c, err)
	}
	ctx, cancel := context.WithTimeout(c.Context(), 25*time.Second)
	defer cancel()
	var body deploymentBody
	if err := c.Bind().Body(&body); err != nil {
		return c.Status(400).JSON(fiber.Map{"error": "invalid body"})
	}
	req := &deploymentv1.DeployModelRequest{IdempotencyKey: key, ModelVersionId: body.ModelVersionID, Resources: body.Resources.proto(), Serving: body.Serving.proto()}
	var res *deploymentv1.DeployModelResponse
	if req.GetServing().GetProfile() == "surya-ocr-v2" {
		res, err = a.clients.Deployment.CreateOCRDeployment(ctx, req)
	} else if strings.EqualFold(strings.TrimSpace(req.GetServing().GetEngine()), "vllm") {
		res, err = a.clients.Deployment.CreateServingDeployment(ctx, req)
	} else {
		res, err = a.clients.Deployment.DeployModel(ctx, req)
	}
	if err != nil {
		return grpcError(c, err)
	}
	return c.Status(202).JSON(fiber.Map{"deployment_id": res.GetDeploymentId(), "operation_id": res.GetOperationId(), "status": res.GetStatus()})
}
func (a *API) getDeployment(c fiber.Ctx) error {
	res, err := a.clients.Deployment.GetDeployment(c.Context(), &deploymentv1.GetDeploymentRequest{DeploymentId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.JSON(deploymentMap(res))
}

func grpcError(c fiber.Ctx, err error) error {
	st := status.Convert(err)
	httpStatus := 500
	switch st.Code() {
	case codes.InvalidArgument:
		httpStatus = 400
	case codes.NotFound:
		httpStatus = 404
	case codes.AlreadyExists:
		httpStatus = 409
	case codes.Aborted, codes.FailedPrecondition:
		httpStatus = 409
	case codes.DeadlineExceeded:
		httpStatus = 504
	case codes.Unavailable:
		httpStatus = 503
	}
	message := strings.TrimSpace(st.Message())
	if message == "" {
		message = "internal error"
	}
	return c.Status(httpStatus).JSON(fiber.Map{"error": message, "grpc_code": st.Code().String()})
}

// Preserve a useful object for new results; do not invent a result for older runs.
func parseResultJSON(raw string) any {
	if strings.TrimSpace(raw) == "" {
		return nil
	}
	var value any
	if err := json.Unmarshal([]byte(raw), &value); err != nil {
		return raw
	}
	return value
}

// Idempotency-Key is optional for old clients. If present it must have exactly
// one value; a missing header and an explicitly empty one are different cases.
func requestKey(c fiber.Ctx) (string, error) {
	var values []string
	for name, entries := range c.GetReqHeaders() {
		if strings.EqualFold(name, "Idempotency-Key") {
			values = append(values, entries...)
		}
	}
	if len(values) == 0 {
		return "", nil
	}
	if len(values) != 1 || values[0] == "" {
		return "", status.Error(codes.InvalidArgument, "provide exactly one nonempty Idempotency-Key")
	}
	if err := idempotency.ValidateKey(values[0]); err != nil {
		return "", err
	}
	return strings.Clone(values[0]), nil
}

func deploymentMap(r *deploymentv1.Deployment) fiber.Map {
	return fiber.Map{
		"deployment_id": r.GetDeploymentId(), "model_version_id": r.GetModelVersionId(), "operation_id": r.GetOperationId(),
		"status": r.GetStatus(), "resources": resourcesMap(r.GetResources()), "submission_status": r.GetSubmissionStatus(), "submission_error_code": r.GetSubmissionErrorCode(),
		"serving":      fiber.Map{"engine": r.GetServing().GetEngine(), "profile": r.GetServing().GetProfile(), "max_model_len": r.GetServing().GetMaxModelLen(), "gpu_memory_utilization": r.GetServing().GetGpuMemoryUtilization()},
		"apply_status": r.GetApplyStatus(), "runtime_state": r.GetRuntimeState(), "desired_state": r.GetDesiredState(),
		"endpoint": r.GetEndpoint(), "internal_endpoint": r.GetInternalEndpoint(), "served_model_name": r.GetServedModelName(),
		"artifact_kind": r.GetArtifactKind(), "base_model_version_id": r.GetBaseModelVersionId(), "adapter_model_version_id": r.GetAdapterModelVersionId(), "base_served_model_name": r.GetBaseServedModelName(),
		"deleted_at": optionalTime(r.GetDeletedAt()),
        "runtime_container_id": r.GetRuntimeContainerId(), "runtime_image_id": r.GetRuntimeImageId(),
		"runtime_error_code": r.GetRuntimeErrorCode(), "runtime_error_message": r.GetRuntimeErrorMessage(),
	}
}
func (a *API) stopDeployment(c fiber.Ctx) error {
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	result, err := a.clients.Deployment.StopDeployment(ctx, &deploymentv1.StopDeploymentRequest{DeploymentId: c.Params("id")})
	if err != nil {
		return grpcError(c, err)
	}
	return c.Status(202).JSON(deploymentMap(result))
}
