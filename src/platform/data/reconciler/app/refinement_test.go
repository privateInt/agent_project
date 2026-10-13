package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/canonical"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
	"strings"
	"testing"
)

type fakeRefinement struct {
	refinementv1.RefinementServiceClient
	wrong bool
}

func (f fakeRefinement) GetRun(context.Context, *refinementv1.GetRunRequest, ...grpc.CallOption) (*refinementv1.RefinementRun, error) {
	return &refinementv1.RefinementRun{RefinementRunId: "run", OperationId: "op", SourceDocumentId: "source", OutputDocumentId: "doc", RulesVersion: canonical.Rules}, nil
}
func (f fakeRefinement) GetDocument(context.Context, *refinementv1.GetDocumentRequest, ...grpc.CallOption) (*refinementv1.CanonicalDocument, error) {
	source := "source"
	if f.wrong {
		source = "different"
	}
	return &refinementv1.CanonicalDocument{DocumentId: "doc", RefinementRunId: "run", SourceDocumentId: source, State: "needs_ocr", SchemaVersion: canonical.Schema, ProcessorVersion: canonical.Processor, RulesVersion: canonical.Rules, Sha256: strings.Repeat("a", 64), SizeBytes: 123}, nil
}
func TestRefinementRegisteredResultRepairsOnlyMatchingCompletion(t *testing.T) {
	ops := &fakeOps{}
	c := Controller{Operations: ops, Refinement: fakeRefinement{}, Docker: fakeDocker{}}
	op := &operationv1.Operation{OperationId: "op", ResourceId: "run", ExecutionType: "refinement.parse", ResourceType: "refinement_run", DispatchProtocol: 1, Status: "running", ExecutionStartedAt: timestamppb.Now()}
	if e := c.Reconcile(context.Background(), op); e != nil || ops.target != "succeeded" || ops.recoveries != 0 {
		t.Fatal("lost completion uncorrected or reran parser", e)
	}
	c.Refinement = fakeRefinement{wrong: true}
	if e := c.Reconcile(context.Background(), op); e != nil || ops.target != "" {
		t.Fatal("wrong source completed", e)
	}
}
