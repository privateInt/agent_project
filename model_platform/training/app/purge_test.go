package app

import (
	"context"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	trainingv1 "model-platform/gen/training/v1"
	"testing"
)

func TestModelPurgeChecksTrainingStopAndFencesOutputRecovery(t *testing.T) {
	f := newOutputFixture(t)
	ctx := context.Background()
	r := &trainingv1.ModelPurgeFence{ModelVersionId: f.r.SourceID}
	f.control.fail = true
	if _, e := f.s.BlockModelPurge(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("active producer accepted", e)
	}
	f.control.fail = false
	f.op.op.Status = "running"
	if _, e := f.s.BlockModelPurge(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("active operation accepted", e)
	}
	f.op.op.Status = "failed"
	if _, e := f.s.BlockModelPurge(ctx, r); e != nil {
		t.Fatal(e)
	}
	if _, e := f.s.RetryTrainingOutput(ctx, &trainingv1.RetryTrainingOutputRequest{TrainingRunId: f.r.RunID, IdempotencyKey: uuid.NewString()}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("new output publication passed purge fence", e)
	}
}

func TestModelPurgeFencesCheckpointResumeWithoutErasingCheckpoint(t *testing.T) {
	f, _ := checkpointFixture(t)
	ctx := context.Background()
	if _, e := f.s.BlockModelPurge(ctx, &trainingv1.ModelPurgeFence{ModelVersionId: f.r.SourceID}); e != nil {
		t.Fatal(e)
	}
	checkpoints, e := f.s.ListTrainingCheckpoints(ctx, &trainingv1.GetTrainingRunRequest{TrainingRunId: f.r.RunID})
	if e != nil || len(checkpoints.Checkpoints) != 1 || checkpoints.Checkpoints[0].Resumable {
		t.Fatal("purged base still resumable", checkpoints, e)
	}
	if _, e := f.s.ResumeTraining(ctx, &trainingv1.ResumeTrainingRequest{TrainingRunId: f.r.RunID, CheckpointStep: 2, IdempotencyKey: uuid.NewString()}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("resume bypassed purge fence", e)
	}
}
