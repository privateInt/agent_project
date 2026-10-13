package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"testing"
	"time"
)

func TestDeleteRunRequiresTerminalIdentityAndPreservesCanonical(t *testing.T) {
	s, ops, store, _ := fixture(t)
	ctx := context.Background()
	r := prepare(t, s, ops, store)
	run, e := s.CompleteRun(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	target := &refinementv1.GetRunRequest{RefinementRunId: r.RefinementRunId}
	if _, e = s.DeleteRun(ctx, target); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("running result hidden", e)
	}
	before, e := s.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: run.OutputDocumentId})
	if e != nil {
		t.Fatal(e)
	}
	if _, e = ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId, Phase: "completed"}); e != nil {
		t.Fatal(e)
	}
	opens := store.opens
	removed, e := s.DeleteRun(ctx, target)
	if e != nil || removed.GetDeletedAt() == nil {
		t.Fatal(e)
	}
	if e = InitSchema(ctx, s.pool); e != nil {
		t.Fatal(e)
	}
	s.ops = nil
	replay, e := s.DeleteRun(ctx, target)
	if e != nil || !proto.Equal(removed.DeletedAt, replay.GetDeletedAt()) {
		t.Fatal("retry depended on operation service", e)
	}
	listed, e := s.ListRuns(ctx, &refinementv1.ListRunsRequest{Q: "refine"})
	if e != nil || len(listed.Items) != 0 {
		t.Fatal("deleted run listed", e)
	}
	after, e := s.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: run.OutputDocumentId})
	if e != nil || !proto.Equal(before, after) || store.opens != opens {
		t.Fatal("canonical was modified", e)
	}
	current, e := s.GetRun(ctx, target)
	if e != nil || current.OutputDocumentId != before.DocumentId || current.GetDeletedAt() == nil {
		t.Fatal("lost result provenance", e)
	}
	if _, e = s.DeleteRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: uuid.NewString()}); status.Code(e) != codes.NotFound {
		t.Fatal(e)
	}
	if _, e = s.DeleteRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: "bad"}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
}

func TestDeletedSourceRejectsNewRefinementButReplaysOriginalRequest(t *testing.T) {
	s, _, _, col := fixture(t)
	ctx := context.Background()
	input := request()
	accepted, e := s.CreateRun(ctx, input)
	if e != nil {
		t.Fatal(e)
	}
	col.deleted = timestamppb.New(time.Now())
	again, e := s.CreateRun(ctx, input)
	if e != nil || !proto.Equal(accepted, again) {
		t.Fatal("original request no longer replays", e)
	}
	fresh := proto.Clone(input).(*refinementv1.CreateRunRequest)
	fresh.IdempotencyKey = uuid.NewString()
	if _, e = s.CreateRun(ctx, fresh); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("new job from deleted source", e)
	}
	pending := &refinementv1.GetRunRequest{RefinementRunId: accepted.RefinementRunId}
	if _, e = s.DeleteRun(ctx, pending); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("not-yet-delivered run hidden", e)
	}
	for _, table := range []string{"refinement_runs", "operation_submissions", "api_requests"} {
		var count int
		if e = s.pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&count); e != nil || count != 1 {
			t.Fatal("failed request left rows", table, count, e)
		}
	}
}

func TestRemovableRunChecksTerminalStateAndResourceIdentity(t *testing.T) {
	run := &refinementv1.RefinementRun{RefinementRunId: uuid.NewString(), OperationId: uuid.NewString()}
	op := &operationv1.Operation{OperationId: run.OperationId, ResourceId: run.RefinementRunId, ResourceType: "refinement_run", ExecutionType: "refinement.parse"}
	for _, state := range []string{"created", "queued", "running", "", "succeeded", "failed", "cancelled"} {
		op.Status = state
		expected := state == "succeeded" || state == "failed" || state == "cancelled"
		if removableRun(op, run) != expected {
			t.Fatal(state)
		}
	}
	for _, field := range []string{"operation", "resource", "type", "execution"} {
		wrong := proto.Clone(op).(*operationv1.Operation)
		switch field {
		case "operation":
			wrong.OperationId = uuid.NewString()
		case "resource":
			wrong.ResourceId = uuid.NewString()
		case "type":
			wrong.ResourceType = "crawl_run"
		case "execution":
			wrong.ExecutionType = "collection.crawl"
		}
		if removableRun(wrong, run) {
			t.Fatal(field)
		}
	}
	if removableRun(nil, run) || removableRun(op, nil) {
		t.Fatal("nil accepted")
	}
}
