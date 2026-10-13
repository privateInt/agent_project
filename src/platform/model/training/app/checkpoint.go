package app

import (
	"context"
	"encoding/json"
	"os"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/submission"
	"model-platform/internal/trainingcheckpoint"
)

type CheckpointControl interface {
	CheckpointStopped(context.Context, *operationv1.Operation) error
}

func checkpointMatches(r trainingcheckpoint.Receipt, run *trainingv1.TrainingRun, op *operationv1.Operation, node string) bool {
	var training TrainingSpec
	var dataset DatasetSpec
	evaluation, err := storedEvaluation(r.Evaluation)
	if err != nil {
		return false
	}
	if json.Unmarshal(r.Training, &training) != nil || json.Unmarshal(r.Dataset, &dataset) != nil {
		return false
	}
	return proto.Equal(evaluation, run.GetEvaluation()) && r.RunID == run.GetTrainingRunId() && r.OperationID == op.GetOperationId() && r.DispatchToken == op.GetDispatchToken() && r.InstanceID == op.GetExecutionInstanceId() && r.Node == node && r.SourceID == run.GetSourceModelVersionId() && r.MaxSteps == run.GetTraining().GetMaxSteps() && op.GetExecutionStartedAt() != nil && proto.Equal(run.GetResources(), op.GetResources()) && proto.Equal(training.Proto(), run.GetTraining()) && proto.Equal(dataset.Proto(), run.GetDataset())
}
func resumableRun(run *trainingv1.TrainingRun, op *operationv1.Operation) bool {
	return (run.GetStatus() == "failed" || run.GetStatus() == "cancelled") && run.GetStatus() == op.GetStatus() && run.GetTraining().GetCheckpointSteps() > 0 && run.GetResources().GetGpuCount() == 1 && localTraining(run.GetResources().GetNodeName())
}
func (s *Service) ListTrainingCheckpoints(ctx context.Context, req *trainingv1.GetTrainingRunRequest) (*trainingv1.TrainingCheckpoints, error) {
	id, e := trainingID(req.GetTrainingRunId())
	if e != nil {
		return nil, e
	}
	run, op, dir, e := s.outputIdentity(ctx, id)
	if e != nil {
		return nil, e
	}
	out := &trainingv1.TrainingCheckpoints{TrainingRunId: id, Availability: "none"}
	if dir == "" || run.GetTraining().GetCheckpointSteps() == 0 {
		return out, nil
	}
	steps, e := trainingcheckpoint.Steps(dir)
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, "checkpoint storage unavailable")
	}
	stopped := false
	if resumableRun(run, op) {
		if control, ok := s.outputControl.(CheckpointControl); ok {
			stopped = control.CheckpointStopped(ctx, op) == nil
		}
	}
	var purging bool
	if e = s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM model_purge_fences WHERE model_version_id=$1)`, run.GetSourceModelVersionId()).Scan(&purging); e != nil {
		return nil, idempotency.DBError(e)
	}
	if purging {
		stopped = false
	}
	for _, step := range steps {
		r, digest, e := trainingcheckpoint.Read(dir, step)
		if os.IsNotExist(e) {
			continue
		}
		if e != nil || !checkpointMatches(r, run, op, s.outputNode) {
			return nil, status.Error(codes.FailedPrecondition, "checkpoint receipt invalid")
		}
		out.Checkpoints = append(out.Checkpoints, &trainingv1.TrainingCheckpoint{Step: r.Step, MaxSteps: r.MaxSteps, CheckpointSha256: digest, SizeBytes: r.Size, FileCount: int64(len(r.Files)), Resumable: stopped && step < r.MaxSteps})
	}
	if len(out.Checkpoints) > 0 {
		out.Availability = "retained"
	}
	return out, nil
}

func (s *Service) ResumeTraining(ctx context.Context, req *trainingv1.ResumeTrainingRequest) (*trainingv1.StartTrainingResponse, error) {
	id, e := trainingID(req.GetTrainingRunId())
	if e != nil {
		return nil, e
	}
	if req.GetCheckpointStep() <= 0 || req.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "checkpoint_step and Idempotency-Key are required")
	}
	fingerprint, e := idempotency.Fingerprint(struct {
		Version int
		Parent  string
		Step    int32
	}{1, id, req.GetCheckpointStep()}, nil)
	if e != nil {
		return nil, e
	}
	response := &trainingv1.StartTrainingResponse{}
	e = idempotency.Execute(ctx, s.pool, "training.resume.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		run, op, dir, e := s.outputIdentity(ctx, id)
		if e != nil {
			return e
		}
		if e = purge.RequireAvailable(ctx, tx, run.GetSourceModelVersionId()); e != nil {
			return e
		}
		if !resumableRun(run, op) {
			return status.Error(codes.FailedPrecondition, "resume requires failed or cancelled local checkpoint training")
		}
		if run.GetDataset().GetSourceType() != "synthetic" && run.GetDataset().GetSourceType() != "version" {
			return status.Error(codes.FailedPrecondition, "resume requires a stable dataset")
		}
		control, ok := s.outputControl.(CheckpointControl)
		if !ok || control.CheckpointStopped(ctx, op) != nil {
			return status.Error(codes.FailedPrecondition, "original Job stop is unconfirmed")
		}
		r, digest, e := trainingcheckpoint.Read(dir, req.GetCheckpointStep())
		if e != nil || !checkpointMatches(r, run, op, s.outputNode) || r.Step >= r.MaxSteps {
			return status.Error(codes.FailedPrecondition, "selected checkpoint is unavailable, incompatible or already complete")
		}
		lookup, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		mv, e := s.clients.Management.GetModelVersion(lookup, &managementv1.GetModelVersionRequest{ModelVersionId: run.GetSourceModelVersionId()})
		if e != nil {
			return status.Error(codes.Unavailable, "source model lookup unavailable")
		}
		if mv.GetModelVersionId() != run.GetSourceModelVersionId() || mv.GetState() != "ready" || mv.GetArtifactKind() == "adapter" {
			return status.Error(codes.FailedPrecondition, "original base snapshot is not ready")
		}
		// Lock and copy immutable normalized specs verbatim. No schedule extension,
		// input override, or reactivation of the original cancelled Operation.
		var state string
		if e = tx.QueryRow(ctx, `SELECT status FROM training_runs WHERE training_run_id=$1 FOR UPDATE`, id).Scan(&state); e != nil {
			return idempotency.DBError(e)
		}
		if state != run.GetStatus() {
			return status.Error(codes.FailedPrecondition, "original training state changed")
		}
		resume, _ := json.Marshal(trainingcheckpoint.Resume{SourceRunID: id, Step: r.Step, SHA256: digest})
		child, operationID := uuid.NewString(), uuid.NewString()
		_, e = tx.Exec(ctx, `INSERT INTO training_runs(training_run_id,source_model_version_id,status,resources,dataset_spec,training_spec,resume_spec,evaluation_spec)
    SELECT $1,source_model_version_id,'created',resources,dataset_spec,training_spec,$3::jsonb,evaluation_spec FROM training_runs WHERE training_run_id=$2`, child, id, resume)
		if e != nil {
			return idempotency.DBError(e)
		}
		if e = submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: operationID, Kind: "training.run", ExecutionType: "training.run", ResourceType: "training_run", ResourceId: child, MaxAttempts: 1, Resources: run.GetResources()}); e != nil {
			return e
		}
		response.TrainingRunId = child
		response.OperationId = operationID
		response.Status = "pending"
		return nil
	})
	if e != nil {
		return nil, e
	}
	s.submissions().Try(ctx, response.GetOperationId())
	return response, nil
}
