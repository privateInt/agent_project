package app

import (
	"context"
	"encoding/json"
	"testing"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"knowledge-platform/dispatcher/executor"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/indexing"
)

type recoveryOperations struct {
	operationv1.OperationServiceClient
	notes     []*operationv1.ApplyReconciliationRequest
	loseReply bool
}

func (f *recoveryOperations) ApplyReconciliation(_ context.Context, r *operationv1.ApplyReconciliationRequest, _ ...grpc.CallOption) (*operationv1.Operation, error) {
	f.notes = append(f.notes, proto.Clone(r).(*operationv1.ApplyReconciliationRequest))
	if f.loseReply {
		f.loseReply = false
		return nil, status.Error(codes.Unavailable, "observation reply lost")
	}
	return &operationv1.Operation{}, nil
}

type recoveryIndex struct {
	indexingv1.IndexingServiceClient
	v indexing.Version
}

func (f *recoveryIndex) GetVersion(context.Context, *indexingv1.VersionRequest, ...grpc.CallOption) (*indexingv1.JSONResponse, error) {
	raw, e := json.Marshal(f.v)
	return &indexingv1.JSONResponse{Json: raw}, e
}

type recoveryObserver struct{ calls int }

func (f *recoveryObserver) Inspect(context.Context, string, string, string) (*executor.Runtime, error) {
	f.calls++
	runtime := &executor.Runtime{}
	runtime.State.Status = "exited"
	return runtime, nil
} // An exited process alone is not a completion receipt.

func TestIndexReconciliationAfterLostFinalReport(t *testing.T) {
	for _, mode := range []string{"ready", "partial_exited", "references_unconfirmed", "identity_changed", "report_reply_lost"} {
		t.Run(mode, func(t *testing.T) {
			op := &operationv1.Operation{OperationId: uuid.NewString(), ResourceId: uuid.NewString(), ResourceType: "index_version", ExecutionType: "indexing.build", DispatchProtocol: 1, ExecutionStartedAt: timestamppb.Now(), Status: "running", Attempt: 1, UpdatedAt: timestamppb.Now()}
			v := indexing.Version{ID: op.ResourceId, OperationID: op.OperationId, State: "ready", Count: 2, Next: 2, ReferenceState: "held"}
			target := "succeeded"
			inspects := 0
			switch mode {
			case "partial_exited":
				v.State = "indexing"
				v.Next = 1
				target = ""
				inspects = 1
			case "references_unconfirmed":
				v.ReferenceState = "pending"
				target = ""
				inspects = 1
			case "identity_changed":
				v.OperationID = uuid.NewString()
				target = ""
			}
			ops := &recoveryOperations{loseReply: mode == "report_reply_lost"}
			observer := &recoveryObserver{}
			controller := Controller{Operations: ops, Indexes: &recoveryIndex{v: v}, Docker: observer}
			e := controller.Reconcile(context.Background(), op)
			if mode == "report_reply_lost" {
				if status.Code(e) != codes.Unavailable {
					t.Fatal("lost observation reply accepted", e)
				}
				if e = controller.Reconcile(context.Background(), op); e != nil {
					t.Fatal(e)
				}
				if len(ops.notes) != 2 || !proto.Equal(ops.notes[0], ops.notes[1]) {
					t.Fatal("retry changed observed operation generation")
				}
			} else if e != nil || len(ops.notes) != 1 {
				t.Fatal("unexpected reconciliation", e, len(ops.notes))
			}
			if observer.calls != inspects {
				t.Fatal("ready receipt required Docker or invalid receipt skipped observation", observer.calls)
			}
			for _, note := range ops.notes {
				if note.TargetStatus != target || note.OperationId != op.OperationId || note.ExpectedStatus != op.Status || note.ExpectedAttempt != op.Attempt || !proto.Equal(note.ExpectedUpdatedAt, op.UpdatedAt) {
					t.Fatal("receipt or CAS identity differs", note)
				}
			}
		})
	}
}
