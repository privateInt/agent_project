package app

import (
	"context"
	"encoding/json"
	"strings"
	"time"

	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/execution"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/remotenode"
	"model-platform/internal/submission"
	"model-platform/internal/trainingevaluation"
	"os"

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

func (s *Service) StartTraining(ctx context.Context, req *trainingv1.StartTrainingRequest) (*trainingv1.StartTrainingResponse, error) {
	modelID, err := uuid.Parse(strings.TrimSpace(req.GetSourceModelVersionId()))
	if err != nil || modelID == uuid.Nil {
		return nil, status.Error(codes.InvalidArgument, "source_model_version_id must be UUID")
	}
	modelVersionID := modelID.String()
	resources, err := execution.NormalizeResources(req.GetResources(), 1)
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid resources: %v", err)
	}
	if resources.GPUCount < 1 || resources.GPUCount > 16 {
		return nil, status.Error(codes.InvalidArgument, "training requires between 1 and 16 GPUs on one host")
	}
	if resources.GPUCount > 1 && len(resources.GPUIds) != int(resources.GPUCount) {
		return nil, status.Error(codes.InvalidArgument, "multi-GPU training requires exact gpu_ids on one host")
	}
	dataset, err := NormalizeDatasetSpec(req.GetDataset())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid dataset: %v", err)
	}
	training, err := NormalizeTrainingSpec(req.GetTraining())
	if err != nil {
		return nil, status.Errorf(codes.InvalidArgument, "invalid training spec: %v", err)
	}
	if resources.GPUCount > 1 {
		if strings.EqualFold(strings.TrimSpace(req.GetTraining().GetStrategy()), "single") {
			return nil, status.Error(codes.InvalidArgument, "single strategy cannot use multiple GPUs")
		}
		training.Strategy = "ddp"
	} else if training.Strategy == "ddp" {
		return nil, status.Error(codes.InvalidArgument, "ddp requires at least two GPUs")
	}
	if training.CheckpointSteps > 0 {
		if resources.GPUCount != 1 || (dataset.SourceType != "synthetic" && dataset.SourceType != "version") {
			return nil, status.Error(codes.InvalidArgument, "checkpoint training requires a local single GPU and synthetic or version-pinned dataset")
		}
	}
	evaluation, err := normalizeEvaluation(req.GetEvaluation())
	if err != nil {
		return nil, err
	}
	if evaluation != nil && (resources.GPUCount != 1 || dataset.SourceType != "version" || training.SamplePacking || training.SequenceLength < 2 || training.SequenceLength > trainingevaluation.MaxSequenceLength) {
		return nil, status.Error(codes.InvalidArgument, "evaluation requires a local single GPU, version-pinned training dataset, unpacked samples and sequence_length 2..4096")
	}
	fingerprint, err := idempotency.Fingerprint(struct {
		Version        int
		ModelVersionID string
		Resources      execution.Resources
		Dataset        DatasetSpec
		Training       TrainingSpec
		Evaluation     *trainingv1.EvaluationSpec `json:"Evaluation,omitempty"`
	}{1, modelVersionID, resources, dataset, training, evaluation}, nil)
	if err != nil {
		return nil, err
	}
	resourcesJSON, err := json.Marshal(resources)
	if err != nil {
		return nil, err
	}
	trainingJSON, err := json.Marshal(training)
	if err != nil {
		return nil, err
	}
	response := &trainingv1.StartTrainingResponse{}
	err = idempotency.Execute(ctx, s.pool, "training.create.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		if training.CheckpointSteps > 0 {
			if !localTraining(resources.NodeName) {
				return status.Error(codes.InvalidArgument, "checkpoint training requires the local node")
			}
			if s.outputRoot == "" {
				return status.Error(codes.Unavailable, "checkpoint output storage unavailable")
			}
		}
		if evaluation != nil {
			if !localTraining(resources.NodeName) {
				return status.Error(codes.InvalidArgument, "evaluation requires the local node")
			}
			if s.outputRoot == "" {
				return status.Error(codes.Unavailable, "evaluation output storage unavailable")
			}
		}
		// Admission only applies to new requests; saved replays survive disabling a node.
		nodes, nodeErr := remotenode.Load()
		if nodeErr != nil {
			return status.Error(codes.Unavailable, "execution node configuration unavailable")
		}
		if nodeErr = nodes.ValidateTraining(resources.NodeName, resources.GPUCount); nodeErr != nil {
			return status.Error(codes.InvalidArgument, nodeErr.Error())
		}
		if resources.NodeName != "" && resources.NodeName != strings.TrimSpace(os.Getenv("DISPATCHER_NODE_NAME")) && len(resources.GPUIds) != 1 {
			return status.Error(codes.InvalidArgument, "remote training requires one exact gpu_id")
		}
		pinnedDataset, err := pinDataset(ctx, tx, dataset)
		if err != nil {
			return err
		}
		var pinnedEvaluation *trainingv1.EvaluationSpec
		if evaluation != nil {
			normalized, _ := NormalizeDatasetSpec(evaluation.GetDataset())
			pinned, err := pinDataset(ctx, tx, normalized)
			if err != nil {
				return err
			}
			if pinned.DatasetVersionID == pinnedDataset.DatasetVersionID || pinned.SHA256 == pinnedDataset.SHA256 {
				return status.Error(codes.InvalidArgument, "training and evaluation dataset content must differ")
			}
			if pinned.RecordCount > trainingevaluation.MaxRecords {
				return status.Error(codes.InvalidArgument, "evaluation dataset exceeds 2048 records")
			}
			pinnedEvaluation = &trainingv1.EvaluationSpec{Dataset: pinned.Proto()}
		}
		evaluationJSON, err := json.Marshal(pinnedEvaluation)
		if err != nil {
			return err
		}
		datasetJSON, err := json.Marshal(pinnedDataset)
		if err != nil {
			return err
		}
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
			return status.Error(codes.FailedPrecondition, "source model version is not ready")
		}
		if mv.GetArtifactKind() == "adapter" {
			return status.Error(codes.FailedPrecondition, "adapter continuation is not implemented")
		}
		id, operationID := uuid.NewString(), uuid.NewString()
		if _, err := tx.Exec(ctx, `INSERT INTO training_runs(training_run_id,source_model_version_id,status,resources,dataset_spec,training_spec,evaluation_spec) VALUES($1,$2,'created',$3::jsonb,$4::jsonb,$5::jsonb,$6::jsonb)`, id, modelVersionID, resourcesJSON, datasetJSON, trainingJSON, evaluationJSON); err != nil {
			return err
		}
		if err := submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: operationID, Kind: "training.run", ExecutionType: "training.run", ResourceType: "training_run", ResourceId: id, MaxAttempts: 1, Resources: resources.Proto()}); err != nil {
			return err
		}
		response.TrainingRunId, response.OperationId, response.Status = id, operationID, "pending"
		return nil
	})
	if err != nil {
		return nil, err
	}
	s.submissions().Try(ctx, response.GetOperationId())
	return response, nil
}
