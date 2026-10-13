package app

import (
	"context"
	"encoding/json"
	"errors"
	"model-platform/internal/trainingcheckpoint"
	"model-platform/internal/trainingoutput"
	"strings"
	"time"

	managementv1 "model-platform/gen/management/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/execution"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/submission"
	"model-platform/internal/trainingparallel"
	"model-platform/training/grpcclient"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Service struct {
	trainingv1.UnimplementedTrainingServiceServer
	pool                   *pgxpool.Pool
	clients                *grpcclient.Clients
	datasetStore           DatasetStore
	jobControl             JobControl
	trackingSync           func(context.Context, string) error
	outputRoot, outputNode string
	outputStore            func(context.Context) (trainingoutput.Store, error)
	outputControl          OutputControl
}

func New(pool *pgxpool.Pool, clients *grpcclient.Clients) *Service {
	return &Service{pool: pool, clients: clients}
}

func InitSchema(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, `
CREATE TABLE IF NOT EXISTS training_runs (
    training_run_id UUID PRIMARY KEY,
    source_model_version_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL,
    resources JSONB NOT NULL DEFAULT '{}'::jsonb,
    dataset_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    training_spec JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_json TEXT NOT NULL DEFAULT '',
    mlflow_experiment_id TEXT NOT NULL DEFAULT '',
    mlflow_run_id TEXT NOT NULL DEFAULT '',
    mlflow_run_url TEXT NOT NULL DEFAULT '',
    output_model_version_id UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT training_status_check CHECK(status IN ('created','running','succeeded','failed'))
);
CREATE INDEX IF NOT EXISTS training_runs_browse_idx ON training_runs(created_at DESC,training_run_id DESC);
CREATE INDEX IF NOT EXISTS training_runs_model_idx ON training_runs(source_model_version_id,created_at DESC);
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS resume_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS evaluation_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS resources JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS dataset_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS training_spec JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS result_json TEXT NOT NULL DEFAULT '';
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS mlflow_experiment_id TEXT NOT NULL DEFAULT '';
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS mlflow_run_id TEXT NOT NULL DEFAULT '';
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS mlflow_run_url TEXT NOT NULL DEFAULT '';
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS output_model_version_id UUID;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS cancelled_at TIMESTAMPTZ;
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS cancellation_error_code TEXT NOT NULL DEFAULT '';
ALTER TABLE training_runs ADD COLUMN IF NOT EXISTS cancellation_checked_at TIMESTAMPTZ;
ALTER TABLE training_runs DROP CONSTRAINT IF EXISTS training_status_check;
ALTER TABLE training_runs ADD CONSTRAINT training_status_check CHECK(status IN ('created','running','cancelling','cancelled','succeeded','failed'));
CREATE INDEX IF NOT EXISTS training_cancellation_idx ON training_runs(cancellation_checked_at NULLS FIRST,training_run_id) WHERE status='cancelling';
`+purge.Schema+idempotency.Schema+submission.Schema+datasetSchema+outputSchema)
	return err
}

func (s *Service) GetTrainingRun(ctx context.Context, req *trainingv1.GetTrainingRunRequest) (*trainingv1.TrainingRun, error) {
	return s.get(ctx, strings.TrimSpace(req.GetTrainingRunId()))
}

// SetTrainingTracking persists the linkage BEFORE the one-shot trainer starts.
// Repeated calls with the same identity are safe; a different run cannot hijack it.
func (s *Service) SetTrainingTracking(ctx context.Context, req *trainingv1.SetTrainingTrackingRequest) (*trainingv1.TrainingRun, error) {
	id, experiment, runID, runURL := strings.TrimSpace(req.GetTrainingRunId()), strings.TrimSpace(req.GetMlflowExperimentId()), strings.TrimSpace(req.GetMlflowRunId()), strings.TrimSpace(req.GetMlflowRunUrl())
	if _, err := uuid.Parse(id); err != nil {
		return nil, status.Error(codes.InvalidArgument, "valid training_run_id is required")
	}
	if experiment == "" || runID == "" || runURL == "" {
		return nil, status.Error(codes.InvalidArgument, "MLflow experiment, run and URL are required")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin tracking update: %v", err)
	}
	defer tx.Rollback(ctx)
	var oldExperiment, oldRun, oldURL, state string
	err = tx.QueryRow(ctx, `SELECT mlflow_experiment_id,mlflow_run_id,mlflow_run_url,status FROM training_runs WHERE training_run_id=$1 FOR UPDATE`, id).Scan(&oldExperiment, &oldRun, &oldURL, &state)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "training run not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "load training run: %v", err)
	}
	if oldRun != "" && (oldRun != runID || oldExperiment != experiment) {
		return nil, status.Error(codes.AlreadyExists, "training run is linked to a different MLflow run")
	}
	if state == "cancelling" || state == "cancelled" {
		return nil, status.Error(codes.FailedPrecondition, "training cancellation requested")
	}
	if state == "failed" || state == "succeeded" {
		if oldRun != runID {
			return nil, status.Error(codes.FailedPrecondition, "training run is terminal")
		}
	} else {
		_, err = tx.Exec(ctx, `UPDATE training_runs SET mlflow_experiment_id=$2,mlflow_run_id=$3,mlflow_run_url=$4,status='running',updated_at=NOW() WHERE training_run_id=$1`, id, experiment, runID, runURL)
		if err != nil {
			return nil, status.Errorf(codes.Internal, "save tracking linkage: %v", err)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit tracking update: %v", err)
	}
	return s.get(ctx, id)
}

// CompleteTraining is the domain's final commit. It refuses empty/partial
// results and verifies the registered, ready output through Management.
func (s *Service) CompleteTraining(ctx context.Context, req *trainingv1.CompleteTrainingRequest) (*trainingv1.TrainingRun, error) {
	id, outputID, raw := strings.TrimSpace(req.GetTrainingRunId()), strings.TrimSpace(req.GetOutputModelVersionId()), strings.TrimSpace(req.GetResultJson())
	if _, err := uuid.Parse(id); err != nil {
		return nil, status.Error(codes.InvalidArgument, "valid training_run_id is required")
	}
	if _, err := uuid.Parse(outputID); err != nil {
		return nil, status.Error(codes.InvalidArgument, "output_model_version_id is required")
	}
	var result struct {
		TrainExitCode        *int                        `json:"train_exit_code"`
		OutputFileCount      int64                       `json:"output_file_count"`
		OutputModelVersionID string                      `json:"output_model_version_id"`
		OutputArtifactURI    string                      `json:"output_artifact_uri"`
		OutputManifestSHA256 string                      `json:"output_manifest_sha256"`
		MLflowRunID          string                      `json:"mlflow_run_id"`
		DatasetVersionID     string                      `json:"dataset_version_id"`
		DatasetSHA256        string                      `json:"dataset_sha256"`
		DatasetBytes         int64                       `json:"dataset_bytes"`
		DatasetRecordCount   int64                       `json:"dataset_record_count"`
		DatasetSourceType    string                      `json:"dataset_source_type"`
		DatasetSourceURI     string                      `json:"dataset_source_uri"`
		DatasetFormat        string                      `json:"dataset_format"`
		Parallelism          trainingparallel.Result     `json:"parallelism"`
		Checkpoint           trainingcheckpoint.Evidence `json:"checkpoint"`
	}
	if raw == "" || json.Unmarshal([]byte(raw), &result) != nil || result.TrainExitCode == nil || *result.TrainExitCode != 0 || result.OutputFileCount <= 0 || result.OutputModelVersionID != outputID || result.OutputArtifactURI == "" || result.OutputManifestSHA256 == "" {
		return nil, status.Error(codes.InvalidArgument, "complete training requires a successful, verified output result")
	}
	mv, err := s.clients.Management.GetModelVersion(ctx, &managementv1.GetModelVersionRequest{ModelVersionId: outputID})
	if err != nil {
		return nil, status.Errorf(codes.FailedPrecondition, "verify registered output: %v", err)
	}
	if mv.GetState() != "ready" || mv.GetArtifactKind() != "adapter" || mv.GetTrainingRunId() != id || mv.GetArtifactUri() != result.OutputArtifactURI || mv.GetArtifactManifestSha256() != result.OutputManifestSHA256 || mv.GetArtifactFileCount() != result.OutputFileCount {
		return nil, status.Error(codes.FailedPrecondition, "registered output is not ready or does not match result")
	}
	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "begin training completion: %v", err)
	}
	defer tx.Rollback(ctx)
	var source, state, oldResult, oldMLflow string
	var datasetJSON, resourcesJSON, trainingJSON []byte
	err = tx.QueryRow(ctx, `SELECT source_model_version_id,status,result_json,mlflow_run_id,dataset_spec,resources,training_spec FROM training_runs WHERE training_run_id=$1 FOR UPDATE`, id).Scan(&source, &state, &oldResult, &oldMLflow, &datasetJSON, &resourcesJSON, &trainingJSON)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "training run not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "load training run: %v", err)
	}
	if source != mv.GetSourceModelVersionId() {
		return nil, status.Error(codes.FailedPrecondition, "output lineage does not match source")
	}
	var pinned DatasetSpec
	if err := json.Unmarshal(datasetJSON, &pinned); err != nil {
		return nil, status.Error(codes.Internal, "stored dataset spec is invalid")
	}
	if pinned.SourceType == "version" && (result.DatasetVersionID != pinned.DatasetVersionID || result.DatasetSHA256 != pinned.SHA256 || result.DatasetBytes != pinned.SizeBytes || result.DatasetRecordCount != pinned.RecordCount || result.DatasetSourceType != pinned.SourceType || result.DatasetSourceURI != pinned.SourceURI || result.DatasetFormat != pinned.Format) {
		return nil, status.Error(codes.FailedPrecondition, "result dataset does not match the pinned training dataset version")
	}
	if oldMLflow != "" && oldMLflow != result.MLflowRunID {
		return nil, status.Error(codes.FailedPrecondition, "MLflow linkage does not match result")
	}
	var resources execution.Resources
	var spec TrainingSpec
	if json.Unmarshal(resourcesJSON, &resources) != nil || json.Unmarshal(trainingJSON, &spec) != nil {
		return nil, status.Error(codes.Internal, "stored training resources/spec are invalid")
	}
	var evaluationJSON []byte
	if err := tx.QueryRow(ctx, `SELECT evaluation_spec FROM training_runs WHERE training_run_id=$1`, id).Scan(&evaluationJSON); err != nil {
		return nil, idempotency.DBError(err)
	}
	evaluation, evalErr := storedEvaluation(evaluationJSON)
	if evalErr != nil {
		return nil, evalErr
	}
	if err := validateEvaluationResult(raw, id, source, spec.Proto(), evaluation); err != nil {
		return nil, err
	}
	if spec.CheckpointSteps > 0 {
		var resumeJSON []byte
		var resume trainingcheckpoint.Resume
		if err := tx.QueryRow(ctx, `SELECT resume_spec FROM training_runs WHERE training_run_id=$1`, id).Scan(&resumeJSON); err != nil {
			return nil, idempotency.DBError(err)
		}
		if json.Unmarshal(resumeJSON, &resume) != nil {
			return nil, status.Error(codes.Internal, "invalid resume selection")
		}
		if err := trainingcheckpoint.ValidateEvidence(result.Checkpoint, id, spec.MaxSteps, resume); err != nil {
			return nil, status.Error(codes.FailedPrecondition, err.Error())
		}
	}
	if resources.GPUCount > 1 {
		batch := int64(resources.GPUCount) * int64(spec.MicroBatchSize) * int64(spec.GradientAccumulationSteps)
		if err := trainingparallel.Validate(result.Parallelism, id, int(resources.GPUCount), int64(spec.MaxSteps), batch); err != nil {
			return nil, status.Error(codes.FailedPrecondition, err.Error())
		}
	}
	if state == "succeeded" {
		if oldResult != raw {
			return nil, status.Error(codes.AlreadyExists, "completed training result is immutable")
		}
	} else if state == "failed" || state == "cancelling" || state == "cancelled" {
		return nil, status.Error(codes.FailedPrecondition, "failed or cancelled training cannot be completed")
	} else {
		_, err = tx.Exec(ctx, `UPDATE training_runs SET status='succeeded',result_json=$2,output_model_version_id=$3,updated_at=NOW() WHERE training_run_id=$1`, id, raw, outputID)
		if err != nil {
			return nil, status.Errorf(codes.Internal, "complete training: %v", err)
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, status.Errorf(codes.Internal, "commit training completion: %v", err)
	}
	return s.get(ctx, id)
}

func (s *Service) FailTraining(ctx context.Context, req *trainingv1.FailTrainingRequest) (*trainingv1.TrainingRun, error) {
	id := strings.TrimSpace(req.GetTrainingRunId())
	if _, err := uuid.Parse(id); err != nil {
		return nil, status.Error(codes.InvalidArgument, "valid training_run_id is required")
	}
	raw := strings.TrimSpace(req.GetResultJson())
	if raw != "" {
		var value map[string]any
		if json.Unmarshal([]byte(raw), &value) != nil || value == nil {
			return nil, status.Error(codes.InvalidArgument, "result_json must be a JSON object")
		}
	}
	tag, err := s.pool.Exec(ctx, `UPDATE training_runs SET status='failed',result_json=CASE WHEN $2='' THEN result_json ELSE $2 END,updated_at=NOW() WHERE training_run_id=$1 AND status IN ('created','running')`, id, raw)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "fail training: %v", err)
	}
	if tag.RowsAffected() == 0 {
		run, e := s.get(ctx, id)
		if e != nil {
			return nil, e
		}
		if run.GetStatus() == "succeeded" || run.GetStatus() == "cancelling" || run.GetStatus() == "cancelled" {
			return nil, status.Error(codes.FailedPrecondition, "completed or cancelled training cannot be failed")
		}
		return run, nil
	}
	return s.get(ctx, id)
}

func (s *Service) get(ctx context.Context, id string) (*trainingv1.TrainingRun, error) {
	if id == "" {
		return nil, status.Error(codes.InvalidArgument, "training_run_id is required")
	}
	var source, statusValue, resultJSON, experimentID, mlflowRunID, mlflowURL, outputID string
	var resourcesJSON, datasetJSON, trainingJSON []byte
	var created, updated time.Time
	var cancelRequested, cancelled *time.Time
	var cancellationError string
	var resumeJSON, evaluationJSON []byte
	err := s.pool.QueryRow(ctx, `
SELECT source_model_version_id,status,resources,resume_spec,evaluation_spec,dataset_spec,training_spec,result_json,mlflow_experiment_id,mlflow_run_id,mlflow_run_url,COALESCE(output_model_version_id::text,''),created_at,updated_at,cancel_requested_at,cancelled_at,cancellation_error_code
FROM training_runs WHERE training_run_id=$1
`, id).Scan(&source, &statusValue, &resourcesJSON, &resumeJSON, &evaluationJSON, &datasetJSON, &trainingJSON, &resultJSON, &experimentID, &mlflowRunID, &mlflowURL, &outputID, &created, &updated, &cancelRequested, &cancelled, &cancellationError)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, status.Error(codes.NotFound, "training run not found")
	}
	if err != nil {
		return nil, status.Errorf(codes.Internal, "get training run: %v", err)
	}
	evaluation, evalErr := storedEvaluation(evaluationJSON)
	if evalErr != nil {
		return nil, evalErr
	}
	var resume trainingcheckpoint.Resume
	if json.Unmarshal(resumeJSON, &resume) != nil {
		return nil, status.Error(codes.Internal, "invalid stored resume selection")
	}
	var resources execution.Resources
	if len(resourcesJSON) > 0 {
		if err := json.Unmarshal(resourcesJSON, &resources); err != nil {
			return nil, status.Errorf(codes.Internal, "decode resources: %v", err)
		}
	}
	var datasetSpec DatasetSpec
	if len(datasetJSON) > 0 {
		if err := json.Unmarshal(datasetJSON, &datasetSpec); err != nil {
			return nil, status.Errorf(codes.Internal, "decode dataset spec: %v", err)
		}
	}
	if datasetSpec.SourceType == "" {
		datasetSpec, _ = NormalizeDatasetSpec(nil)
	}
	var trainingSpec TrainingSpec
	if len(trainingJSON) > 0 {
		if err := json.Unmarshal(trainingJSON, &trainingSpec); err != nil {
			return nil, status.Errorf(codes.Internal, "decode training spec: %v", err)
		}
	}
	if trainingSpec.Method == "" {
		trainingSpec, _ = NormalizeTrainingSpec(nil)
	}
	info, err := submission.Lookup(ctx, s.pool, "training_run", id)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	var recoveryStatus, recoveredID string
	err = s.pool.QueryRow(ctx, `SELECT status,output_model_version_id FROM training_output_recoveries WHERE training_run_id=$1 ORDER BY requested_at DESC,recovery_id DESC LIMIT 1`, id).Scan(&recoveryStatus, &recoveredID)
	if err != nil && !errors.Is(err, pgx.ErrNoRows) {
		return nil, idempotency.DBError(err)
	}
	return &trainingv1.TrainingRun{Evaluation: evaluation, Resume: resume.Proto(), OutputRecoveryStatus: recoveryStatus, RecoveredModelVersionId: recoveredID, OperationId: info.OperationID, SubmissionStatus: info.Status, SubmissionErrorCode: info.ErrorCode,
		CancelRequestedAt: optionalTimestamp(cancelRequested), CancelledAt: optionalTimestamp(cancelled), CancellationErrorCode: cancellationError,
		TrainingRunId:        id,
		SourceModelVersionId: source,
		Status:               statusValue,
		CreatedAt:            timestamppb.New(created),
		UpdatedAt:            timestamppb.New(updated),
		Resources:            resources.Proto(),
		ResultJson:           resultJSON,
		MlflowExperimentId:   experimentID,
		MlflowRunId:          mlflowRunID,
		MlflowRunUrl:         mlflowURL,
		OutputModelVersionId: outputID,
		Dataset:              datasetSpec.Proto(),
		Training:             trainingSpec.Proto(),
	}, nil
}
