package app

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/canonical"
	"data-platform/internal/submission"
	"data-platform/internal/testsupport"
	operationapp "data-platform/operation/app"
	"encoding/hex"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"strings"
	"sync"
	"sync/atomic"
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
func (m *memoryStore) URI(k string) string { return "s3://data-canonical/" + k }

type operationBridge struct {
	operationv1.OperationServiceClient
	service *operationapp.Service
}

func (b operationBridge) GetOperation(c context.Context, r *operationv1.GetOperationRequest, _ ...grpc.CallOption) (*operationv1.Operation, error) {
	return b.service.GetOperation(c, r)
}

type collectionBridge struct {
	collectionv1.CollectionServiceClient
	calls   atomic.Int32
	offline bool
	deleted *timestamppb.Timestamp
}

func (b *collectionBridge) GetDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	b.calls.Add(1)
	if b.offline {
		return nil, status.Error(codes.Unavailable, "offline")
	}
	return &collectionv1.SourceDocument{DocumentId: r.DocumentId, State: "ready", DeletedAt: b.deleted, Sha256: strings.Repeat("b", 64), SizeBytes: 5, Format: "text"}, nil
}
func fixture(t *testing.T) (*Service, *operationapp.Service, *memoryStore, *collectionBridge) {
	t.Helper()
	p := testsupport.Postgres(t)
	ctx := context.Background()
	if e := InitSchema(ctx, p); e != nil {
		t.Fatal(e)
	}
	if e := operationapp.InitSchema(ctx, p); e != nil {
		t.Fatal(e)
	}
	ops := operationapp.New(p)
	store := &memoryStore{}
	col := &collectionBridge{}
	return New(p, operationBridge{service: ops}, col, store), ops, store, col
}
func request() *refinementv1.CreateRunRequest {
	return &refinementv1.CreateRunRequest{Name: "refine source", DocumentId: uuid.NewString(), IdempotencyKey: uuid.NewString()}
}
func TestConcurrentReplayPinsOneSourceAndSurvivesCollectionOutage(t *testing.T) {
	s, _, _, col := fixture(t)
	r := request()
	ctx := context.Background()
	var wg sync.WaitGroup
	results := make(chan *refinementv1.RunAccepted, 6)
	errs := make(chan error, 6)
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			out, e := s.CreateRun(ctx, proto.Clone(r).(*refinementv1.CreateRunRequest))
			results <- out
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
	for out := range results {
		if id == "" {
			id = out.RefinementRunId
		}
		if out.RefinementRunId != id {
			t.Fatal("duplicate refinement")
		}
	}
	if col.calls.Load() != 1 {
		t.Fatal("source read on replay", col.calls.Load())
	}
	col.offline = true
	replay, e := s.CreateRun(ctx, r)
	if e != nil || replay.RefinementRunId != id {
		t.Fatal("replay depended on Collection", e)
	}
	changed := proto.Clone(r).(*refinementv1.CreateRunRequest)
	changed.DocumentId = uuid.NewString()
	if _, e = s.CreateRun(ctx, changed); status.Code(e) != codes.AlreadyExists {
		t.Fatal("different source reused key", e)
	}
	for _, table := range []string{"refinement_runs", "operation_submissions", "api_requests"} {
		var n int
		if e = s.pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&n); e != nil || n != 1 {
			t.Fatal(table, n, e)
		}
	}
}
func TestSubmissionFailureRollsBackAndSameKeyCanRetry(t *testing.T) {
	s, _, _, _ := fixture(t)
	r := request()
	ctx := context.Background()
	if _, e := s.pool.Exec(ctx, `ALTER TABLE operation_submissions ADD CONSTRAINT reject_fixture CHECK(resource_type<>'refinement_run')`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.CreateRun(ctx, r); e == nil {
		t.Fatal("submission failure ignored")
	}
	for _, table := range []string{"refinement_runs", "operation_submissions", "api_requests"} {
		var n int
		if e := s.pool.QueryRow(ctx, "SELECT count(*) FROM "+table).Scan(&n); e != nil || n != 0 {
			t.Fatal("partial request persisted", table, n, e)
		}
	}
	if _, e := s.pool.Exec(ctx, `ALTER TABLE operation_submissions DROP CONSTRAINT reject_fixture`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.CreateRun(ctx, r); e != nil {
		t.Fatal(e)
	}
}
func prepare(t *testing.T, s *Service, ops *operationapp.Service, m *memoryStore) *refinementv1.CompleteRunRequest {
	t.Helper()
	ctx := context.Background()
	input := request()
	run, e := s.CreateRun(ctx, input)
	if e != nil {
		t.Fatal(e)
	}
	worker := submission.Worker{Pool: s.pool, Ensure: ops.EnsureOperation}
	if _, e = worker.Deliver(ctx, run.OperationId); e != nil {
		t.Fatal(e)
	}
	claim, e := ops.ClaimOperation(ctx, &operationv1.ClaimOperationRequest{OperationId: run.OperationId, OwnerId: "test"})
	if e != nil || !claim.GetClaimed() {
		t.Fatal(e)
	}
	instance := uuid.NewString()
	op, e := ops.BeginExecution(ctx, &operationv1.BeginExecutionRequest{OperationId: run.OperationId, ResourceId: run.RefinementRunId, ExecutionType: "refinement.parse", DispatchToken: claim.Operation.DispatchToken, ExecutionInstanceId: instance})
	if e != nil {
		t.Fatal(e)
	}
	image := "sha256:" + strings.Repeat("a", 64)
	raw, _ := json.Marshal(map[string]any{"schema_version": canonical.Schema, "source": map[string]string{"document_id": input.DocumentId, "sha256": strings.Repeat("b", 64), "format": "text"}, "processor": map[string]string{"version": canonical.Processor, "rules_version": canonical.Rules, "image_id": image}, "cleaning": map[string]string{"version": canonical.Rules}, "blocks": []map[string]any{{"id": "b1", "order": 0, "type": "text", "text": "hello"}}, "pages": []any{}, "quality": map[string]any{"state": "needs_ocr", "issues": []map[string]any{{"code": "fixture_visual", "requires_ocr": true}}}})
	m.body = string(raw)
	sum := sha256.Sum256(raw)
	return &refinementv1.CompleteRunRequest{RefinementRunId: run.RefinementRunId, OperationId: run.OperationId, DispatchToken: op.DispatchToken, ExecutionInstanceId: instance, Sha256: hex.EncodeToString(sum[:]), SizeBytes: int64(len(raw)), ProcessorImageId: image}
}
func TestCompletionGateIntegrityImmutableReplayAndQuality(t *testing.T) {
	s, ops, m, _ := fixture(t)
	ctx := context.Background()
	r := prepare(t, s, ops, m)
	bad := proto.Clone(r).(*refinementv1.CompleteRunRequest)
	bad.ExecutionInstanceId = uuid.NewString()
	if _, e := s.CompleteRun(ctx, bad); status.Code(e) != codes.FailedPrecondition || m.opens != 0 {
		t.Fatal("unauthorized output touched storage", e)
	}
	original := m.body
	m.body = "tampered"
	if _, e := s.CompleteRun(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("corrupt output accepted", e)
	}
	m.body = original
	first, e := s.CompleteRun(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	replay, e := s.CompleteRun(ctx, r)
	if e != nil || replay.OutputDocumentId != first.OutputDocumentId {
		t.Fatal("replay changed output", e)
	}
	doc, e := s.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: first.OutputDocumentId})
	if e != nil || doc.State != "needs_ocr" || doc.ProcessorImageId != r.ProcessorImageId {
		t.Fatal("quality/provenance lost", doc, e)
	}
	bad = proto.Clone(r).(*refinementv1.CompleteRunRequest)
	bad.ProcessorImageId = "sha256:" + strings.Repeat("c", 64)
	if _, e = s.CompleteRun(ctx, bad); status.Code(e) != codes.AlreadyExists {
		t.Fatal("immutable result overwritten", e)
	}
	if _, e = ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId, Phase: "completed"}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.CompleteRun(ctx, r); e != nil {
		t.Fatal("completed replay failed", e)
	}
	listed, e := ops.ListOperations(ctx, &operationv1.ListOperationsRequest{ResourceType: "refinement_run", ResourceId: r.RefinementRunId})
	if e != nil || len(listed.Operations) != 1 || listed.Operations[0].Status != "succeeded" {
		t.Fatal("refinement operation filter", e)
	}
}
func TestDifferentSourceProvenanceCannotRegister(t *testing.T) {
	s, ops, m, _ := fixture(t)
	ctx := context.Background()
	r := prepare(t, s, ops, m)
	m.body = strings.Replace(m.body, strings.Repeat("b", 64), strings.Repeat("d", 64), 1)
	sum := sha256.Sum256([]byte(m.body))
	r.Sha256 = hex.EncodeToString(sum[:])
	if _, e := s.CompleteRun(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("wrong source registered", e)
	}
	run, e := s.GetRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: r.RefinementRunId})
	if e != nil || run.OutputDocumentId != "" {
		t.Fatal("partial result", e)
	}
}
func TestRefinementPaginationDoesNotLaunchJobs(t *testing.T) {
	s, _, _, _ := fixture(t)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, e := s.CreateRun(ctx, request()); e != nil {
			t.Fatal(e)
		}
	}
	a, e := s.ListRuns(ctx, &refinementv1.ListRunsRequest{Limit: 2, Q: "refine"})
	if e != nil || len(a.Items) != 2 || a.NextCursor == "" {
		t.Fatal(a, e)
	}
	b, e := s.ListRuns(ctx, &refinementv1.ListRunsRequest{Limit: 2, Q: "refine", Cursor: a.NextCursor})
	if e != nil || len(b.Items) != 1 {
		t.Fatal(b, e)
	}
	if _, e = s.ListRuns(ctx, &refinementv1.ListRunsRequest{Limit: 2, Q: "changed", Cursor: a.NextCursor}); status.Code(e) != codes.InvalidArgument {
		t.Fatal("cross-filter cursor", e)
	}
	var count int
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM operations`).Scan(&count); e != nil || count != 0 {
		t.Fatal("read launched operation", e)
	}
}
func TestNormalizeRefinementVersionAndInput(t *testing.T) {
	r := request()
	out, e := NormalizeRequest(r)
	if e != nil || out.RulesVersion != canonical.Rules {
		t.Fatal(out, e)
	}
	for _, change := range []func(*refinementv1.CreateRunRequest){func(r *refinementv1.CreateRunRequest) { r.RulesVersion = "unknown" }, func(r *refinementv1.CreateRunRequest) { r.DocumentId = "bad" }, func(r *refinementv1.CreateRunRequest) { r.IdempotencyKey = "" }} {
		r := request()
		change(r)
		if _, e = NormalizeRequest(r); status.Code(e) != codes.InvalidArgument {
			t.Fatal(e)
		}
	}
}
