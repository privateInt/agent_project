package app

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/blob"
	"data-platform/internal/crawl"
	"data-platform/internal/submission"
	"data-platform/internal/testsupport"
	operationapp "data-platform/operation/app"
	"encoding/hex"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"io"
	"strings"
	"sync"
	"testing"
)

type memoryStore struct {
	body  string
	opens int
}

func (m *memoryStore) Put(context.Context, string, io.Reader, int64, string) error { return nil }
func (m *memoryStore) Open(context.Context, string) (io.ReadCloser, error) {
	m.opens++
	return io.NopCloser(strings.NewReader(m.body)), nil
}
func (m *memoryStore) URI(k string) string { return "s3://data-sources/" + k }

type operationBridge struct {
	operationv1.OperationServiceClient
	service *operationapp.Service
}

func (b operationBridge) GetOperation(c context.Context, r *operationv1.GetOperationRequest, _ ...grpc.CallOption) (*operationv1.Operation, error) {
	return b.service.GetOperation(c, r)
}
func fixture(t *testing.T) (*Service, *operationapp.Service, *memoryStore) {
	t.Helper()
	pool := testsupport.Postgres(t)
	ctx := context.Background()
	if e := InitSchema(ctx, pool); e != nil {
		t.Fatal(e)
	}
	if e := operationapp.InitSchema(ctx, pool); e != nil {
		t.Fatal(e)
	}
	ops := operationapp.New(pool)
	store := &memoryStore{body: "original"}
	return New(pool, operationBridge{service: ops}, store), ops, store
}
func request() *collectionv1.CreateCrawlRequest {
	return &collectionv1.CreateCrawlRequest{Name: "source", Url: "https://example.com/", IdempotencyKey: uuid.NewString()}
}
func TestRequestReplayAndConcurrentCreationAreAtomic(t *testing.T) {
	s, _, _ := fixture(t)
	ctx := context.Background()
	r := request()
	var wg sync.WaitGroup
	results := make(chan *collectionv1.CrawlAccepted, 8)
	errs := make(chan error, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			result, e := s.CreateCrawl(ctx, proto.Clone(r).(*collectionv1.CreateCrawlRequest))
			results <- result
			errs <- e
		}()
	}
	wg.Wait()
	close(results)
	close(errs)
	for e := range errs {
		if e != nil {
			t.Fatal(e)
		}
	}
	id := ""
	for v := range results {
		if id == "" {
			id = v.CrawlRunId
		}
		if v.CrawlRunId != id {
			t.Fatal("concurrent replay created another run")
		}
	}
	for _, table := range []string{"crawl_runs", "operation_submissions", "api_requests"} {
		var n int
		if e := s.pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&n); e != nil || n != 1 {
			t.Fatal(table, n, e)
		}
	}
	changed := proto.Clone(r).(*collectionv1.CreateCrawlRequest)
	changed.Url = "https://example.com/other"
	if _, e := s.CreateCrawl(ctx, changed); status.Code(e) != codes.AlreadyExists {
		t.Fatal("changed request reused key", e)
	}
}
func TestCrawlAndSubmissionRollbackTogether(t *testing.T) {
	s, _, _ := fixture(t)
	ctx := context.Background()
	if _, e := s.pool.Exec(ctx, `ALTER TABLE operation_submissions ADD CONSTRAINT reject_fixture CHECK(resource_type<>'crawl_run')`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.CreateCrawl(ctx, request()); e == nil {
		t.Fatal("failed outbox write was ignored")
	}
	for _, table := range []string{"crawl_runs", "operation_submissions", "api_requests"} {
		var n int
		if e := s.pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&n); e != nil || n != 0 {
			t.Fatal("partial creation", table, n, e)
		}
	}
}
func prepareResult(t *testing.T, s *Service, ops *operationapp.Service, m *memoryStore) *collectionv1.CompleteCrawlRequest {
	return prepareResultFor(t, s, ops, m, request())
}
func prepareResultFor(t *testing.T, s *Service, ops *operationapp.Service, m *memoryStore, input *collectionv1.CreateCrawlRequest) *collectionv1.CompleteCrawlRequest {
	t.Helper()
	ctx := context.Background()
	run, e := s.CreateCrawl(ctx, input)
	if e != nil {
		t.Fatal(e)
	}
	worker := submission.Worker{Pool: s.pool, Ensure: ops.EnsureOperation}
	if _, e = worker.Deliver(ctx, run.OperationId); e != nil {
		t.Fatal(e)
	}
	claim, e := ops.ClaimOperation(ctx, &operationv1.ClaimOperationRequest{OperationId: run.OperationId, OwnerId: "test-dispatcher"})
	if e != nil || !claim.GetClaimed() {
		t.Fatal("claim", e)
	}
	instance := uuid.NewString()
	op, e := ops.BeginExecution(ctx, &operationv1.BeginExecutionRequest{OperationId: run.OperationId, ResourceId: run.CrawlRunId, ExecutionType: crawl.OptionsFromProto(input.Options).ExecutionType(), DispatchToken: claim.Operation.DispatchToken, ExecutionInstanceId: instance})
	if e != nil {
		t.Fatal(e)
	}
	sum := sha256.Sum256([]byte(m.body))
	return &collectionv1.CompleteCrawlRequest{CrawlRunId: run.CrawlRunId, OperationId: run.OperationId, DispatchToken: op.DispatchToken, ExecutionInstanceId: instance, FinalUrl: "https://example.com/", Sha256: hex.EncodeToString(sum[:]), SizeBytes: int64(len(m.body)), ContentType: "text/html; charset=utf-8", HttpStatus: 200, ProcessorImageId: "sha256:" + strings.Repeat("a", 64)}
}
func TestResultNeedsGateAndStoredIntegrityThenRemainsImmutable(t *testing.T) {
	s, ops, m := fixture(t)
	ctx := context.Background()
	r := prepareResult(t, s, ops, m)
	bad := proto.Clone(r).(*collectionv1.CompleteCrawlRequest)
	bad.ExecutionInstanceId = uuid.NewString()
	if _, e := s.CompleteCrawl(ctx, bad); status.Code(e) != codes.FailedPrecondition || m.opens != 0 {
		t.Fatal("unauthorized result read storage", e)
	}
	m.body = "tampered"
	if _, e := s.CompleteCrawl(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("bad source registered", e)
	}
	run, e := s.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: r.CrawlRunId})
	if e != nil || run.OutputDocumentId != "" {
		t.Fatal("failed result persisted", run, e)
	}
	m.body = "original"
	first, e := s.CompleteCrawl(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	replay, e := s.CompleteCrawl(ctx, proto.Clone(r).(*collectionv1.CompleteCrawlRequest))
	if e != nil || replay.OutputDocumentId != first.OutputDocumentId {
		t.Fatal("lost reply retry changed result", e)
	}
	doc, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: first.OutputDocumentId})
	if e != nil || doc.Sha256 != r.Sha256 || doc.ProcessorImageId != r.ProcessorImageId || doc.ContentType != r.ContentType {
		t.Fatal("source provenance lost", doc, e)
	}
	bad = proto.Clone(r).(*collectionv1.CompleteCrawlRequest)
	bad.FinalUrl = "https://example.com/changed"
	if _, e = s.CompleteCrawl(ctx, bad); status.Code(e) != codes.AlreadyExists {
		t.Fatal("result overwritten", e)
	}
	if _, e = ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId, Phase: "completed"}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.CompleteCrawl(ctx, r); e != nil {
		t.Fatal("completed replay failed", e)
	}
	listed, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{Q: "source", Limit: 1})
	if e != nil || len(listed.Items) != 1 {
		t.Fatal("read-only source list failed", e)
	}
	var n int
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM operations`).Scan(&n); e != nil || n != 1 {
		t.Fatal("read created execution", n, e)
	}
}
func TestCrawlPaginationKeepsFilterAndDoesNotSubmitJobs(t *testing.T) {
	s, _, _ := fixture(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, e := s.CreateCrawl(ctx, request()); e != nil {
			t.Fatal(e)
		}
	}
	a, e := s.ListCrawls(ctx, &collectionv1.ListCrawlsRequest{Limit: 2, Q: "source"})
	if e != nil || len(a.Items) != 2 || a.NextCursor == "" {
		t.Fatal(a, e)
	}
	b, e := s.ListCrawls(ctx, &collectionv1.ListCrawlsRequest{Limit: 2, Q: "source", Cursor: a.NextCursor})
	if e != nil || len(b.Items) != 1 || b.Items[0].CrawlRunId == a.Items[0].CrawlRunId {
		t.Fatal(b, e)
	}
	if _, e = s.ListCrawls(ctx, &collectionv1.ListCrawlsRequest{Limit: 2, Q: "changed", Cursor: a.NextCursor}); status.Code(e) != codes.InvalidArgument {
		t.Fatal("cursor accepted changed filters", e)
	}
	var n int
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM operations`).Scan(&n); e != nil || n != 0 {
		t.Fatal("browsing started jobs", n, e)
	}
}
func TestNormalizeCrawlLimits(t *testing.T) {
	r := request()
	r.Url = "https://EXAMPLE.com:443/#part"
	got, e := NormalizeRequest(r)
	if e != nil || got.Url != "https://example.com/" || got.Options.MaxBytes != 8<<20 {
		t.Fatal(got, e)
	}
	r.Options = &collectionv1.CrawlOptions{MaxBytes: 33 << 20}
	if _, e = NormalizeRequest(r); e == nil {
		t.Fatal("unbounded source accepted")
	}
	r.Options = nil
	r.IdempotencyKey = ""
	if _, e = NormalizeRequest(r); e == nil {
		t.Fatal("keyless mutation accepted")
	}
	if _, e := blob.Key(uuid.NewString(), strings.Repeat("a", 64)); e != nil {
		t.Fatal(e)
	}
}
