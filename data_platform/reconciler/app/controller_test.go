package app

import (
	"context"
	"data-platform/dispatcher/executor"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
	"strings"
	"testing"
	"time"
)

type fakeOps struct {
	operationv1.OperationServiceClient
	recoveries int
	target     string
}

func (f *fakeOps) RecoverDispatch(context.Context, *operationv1.RecoverDispatchRequest, ...grpc.CallOption) (*operationv1.Operation, error) {
	f.recoveries++
	return &operationv1.Operation{}, nil
}
func (f *fakeOps) ApplyReconciliation(_ context.Context, r *operationv1.ApplyReconciliationRequest, _ ...grpc.CallOption) (*operationv1.Operation, error) {
	f.target = r.TargetStatus
	return &operationv1.Operation{}, nil
}

type fakeCollection struct {
	collectionv1.CollectionServiceClient
	doc bool
}

func (f fakeCollection) GetCrawl(context.Context, *collectionv1.GetCrawlRequest, ...grpc.CallOption) (*collectionv1.CrawlRun, error) {
	id := ""
	if f.doc {
		id = "doc"
	}
	return &collectionv1.CrawlRun{CrawlRunId: "run", OperationId: "op", OutputDocumentId: id, Options: &collectionv1.CrawlOptions{MaxBytes: 1024}}, nil
}
func (f fakeCollection) GetDocument(context.Context, *collectionv1.GetDocumentRequest, ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	return &collectionv1.SourceDocument{DocumentId: "doc", CrawlRunId: "run", State: "ready", Sha256: strings.Repeat("a", 64), SizeBytes: 4}, nil
}

type fakeDocker struct{}

func (fakeDocker) Inspect(context.Context, string, string, string) (*executor.Runtime, error) {
	return &executor.Runtime{}, nil
}
func TestOnlyUnstartedDispatchCanRearm(t *testing.T) {
	ops := &fakeOps{}
	c := Controller{Operations: ops, Collection: fakeCollection{}, Docker: fakeDocker{}}
	op := &operationv1.Operation{OperationId: "op", ResourceId: "run", ResourceType: "crawl_run", ExecutionType: "collection.crawl", DispatchProtocol: 1, Status: "dispatching", DispatchGeneration: 1, DispatchLeaseUntil: timestamppb.New(time.Now().Add(-time.Minute))}
	if e := c.Reconcile(context.Background(), op); e != nil || ops.recoveries != 1 {
		t.Fatal(e)
	}
	op.ExecutionStartedAt = timestamppb.Now()
	op.Status = "running"
	if e := c.Reconcile(context.Background(), op); e != nil || ops.recoveries != 1 || ops.target != "" {
		t.Fatal("actual execution was replayed or failed without proof", e)
	}
	c.Collection = fakeCollection{doc: true}
	if e := c.Reconcile(context.Background(), op); e != nil || ops.recoveries != 1 || ops.target != "succeeded" {
		t.Fatal("lost completion report was not repaired", e)
	}
}

type batchCollection struct {
	collectionv1.CollectionServiceClient
	bad   bool
	reads int
}

func (f *batchCollection) GetCrawl(context.Context, *collectionv1.GetCrawlRequest, ...grpc.CallOption) (*collectionv1.CrawlRun, error) {
	return &collectionv1.CrawlRun{CrawlRunId: "run", OperationId: "op", OutputDocumentId: "seed", OutputDocumentIds: []string{"seed", "child"}, Options: &collectionv1.CrawlOptions{FollowLinks: true, MaxBytes: 1024, MaxDocuments: 2, MaxTotalBytes: 2048}}, nil
}
func (f *batchCollection) GetDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	f.reads++
	state := "ready"
	if f.bad && r.DocumentId == "child" {
		state = "pending"
	}
	return &collectionv1.SourceDocument{DocumentId: r.DocumentId, CrawlRunId: "run", State: state, Sha256: strings.Repeat("a", 64), SizeBytes: 10, ProcessorVersion: "http-scope-v1", ProcessorImageId: "sha256:" + strings.Repeat("b", 64)}, nil
}
func TestReconcileRequiresEveryBatchDocument(t *testing.T) {
	op := &operationv1.Operation{OperationId: "op", ResourceId: "run", ResourceType: "crawl_run", ExecutionType: "collection.crawl", DispatchProtocol: 1, Status: "running", ExecutionStartedAt: timestamppb.Now()}
	for _, bad := range []bool{true, false} {
		ops := &fakeOps{}
		collection := &batchCollection{bad: bad}
		c := Controller{Operations: ops, Collection: collection, Docker: fakeDocker{}}
		if e := c.Reconcile(context.Background(), op); e != nil || collection.reads != 2 {
			t.Fatal(e, collection.reads)
		}
		if bad && ops.target != "" || !bad && ops.target != "succeeded" {
			t.Fatal("premature or missing batch completion", ops.target)
		}
	}
}
