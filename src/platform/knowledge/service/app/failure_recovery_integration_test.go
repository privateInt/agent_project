package app

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
)

// Persistent fake side effects survive replacement of the service object.
// The catalog and pending/committed receipts are real PostgreSQL transactions.
type recoveringStore struct {
	indexBackend
	records map[string]indexing.Record
	sealed  bool
}

func (b *recoveringStore) Bulk(_ context.Context, _ indexing.Version, rows []indexing.Record) error {
	b.bulkCalls++
	if b.sealed {
		return fmt.Errorf("write after seal")
	}
	for i, row := range rows {
		if old, ok := b.records[row.ID]; ok && !reflect.DeepEqual(old, row) {
			return fmt.Errorf("replayed document changed")
		}
		b.records[row.ID] = row
		if b.bulkCalls == 2 && i == 0 {
			return fmt.Errorf("partial bulk: first item persisted, response lost")
		}
	}
	return nil
}
func (b *recoveringStore) Seal(_ context.Context, v indexing.Version) error {
	b.sealCalls++
	if len(b.records) != v.Count {
		return fmt.Errorf("physical count differs")
	}
	b.sealed = true
	if b.sealCalls == 1 {
		return fmt.Errorf("write block applied, reply lost")
	}
	return nil
}
func executionFor(v indexing.Version, ops *indexOps) *indexingv1.ExecutionRequest {
	token, instance := uuid.NewString(), uuid.NewString()
	ops.grant = &operationv1.Operation{OperationId: v.OperationID, ResourceId: v.ID, ResourceType: "index_version", ExecutionType: "indexing.build", DispatchProtocol: 1, DispatchToken: token, ExecutionInstanceId: instance, Status: "running", ExecutionStartedAt: timestamppb.Now()}
	return &indexingv1.ExecutionRequest{IndexVersionId: v.ID, OperationId: v.OperationID, DispatchToken: token, ExecutionInstanceId: instance}
}
func currentVersion(t *testing.T, s *IndexService, id string) indexing.Version {
	t.Helper()
	out, e := s.GetVersion(context.Background(), &indexingv1.VersionRequest{IndexVersionId: id})
	if e != nil {
		t.Fatal(e)
	}
	v, e := versionFrom(out.Json)
	if e != nil {
		t.Fatal(e)
	}
	return v
}
func batchReceipt(t *testing.T, s *IndexService, grant *indexingv1.ExecutionRequest, offset int) *indexingv1.CommitBatchRequest {
	t.Helper()
	raw, e := s.ReadBatch(context.Background(), &indexingv1.ReadBatchRequest{Execution: grant, Offset: int32(offset)})
	if e != nil {
		t.Fatal(e)
	}
	var batch indexing.Batch
	if e = json.Unmarshal(raw.Json, &batch); e != nil {
		t.Fatal(e)
	}
	result := &embeddingv1.EmbedResponse{Profile: batch.Version.Profile, Role: "document"}
	for i, chunk := range batch.Chunks {
		vector := make([]float64, 1024)
		vector[0] = 1
		result.Data = append(result.Data, &embeddingv1.EmbeddingOutput{Id: chunk.ID, Index: int32(i), Embedding: vector, InputTokens: 10})
	}
	return &indexingv1.CommitBatchRequest{Execution: grant, Offset: int32(offset), Result: result}
}

func TestIndexPartialBatchAndSealRecovery(t *testing.T) {
	s, m, refs, ops, _, objects, run := indexFixture(t, strings.Repeat("격리된 복구 테스트 문장입니다. ", 600))
	m.p.MaxBatchSize = 2
	backend := &recoveringStore{records: map[string]indexing.Record{}}
	s.search = backend
	ctx := context.Background()
	_, v, _ := newVersion(t, s, m, run)
	if e := s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	grant := executionFor(v, ops)
	if _, e := s.PrepareBuild(ctx, grant); e != nil {
		t.Fatal(e)
	}
	v = currentVersion(t, s, v.ID)
	if v.Count < 5 {
		t.Fatal("fixture must span at least three batches", v.Count)
	}
	first := batchReceipt(t, s, grant, 0)
	if _, e := s.CommitBatch(ctx, first); e != nil {
		t.Fatal(e)
	}
	second := batchReceipt(t, s, grant, 2)
	if _, e := s.CommitBatch(ctx, second); status.Code(e) != codes.Unavailable {
		t.Fatal("partial bulk accepted", e)
	}
	v = currentVersion(t, s, v.ID)
	if v.Next != 2 || v.State != "indexing" || len(backend.records) != 3 || v.ReferenceState != "held" || refs.releases != 0 {
		t.Fatal("partial receipt advanced or released input", v, len(backend.records))
	}
	var committed bool
	var hash string
	if e := s.chunks.pool.QueryRow(ctx, `SELECT committed,receipt_hash FROM index_batches WHERE index_version_id=$1 AND offset_value=2`, v.ID).Scan(&committed, &hash); e != nil {
		t.Fatal(e)
	}
	if committed || hash != indexing.Receipt(second.Result) {
		t.Fatal("pending response digest was not retained")
	}
	if _, e := s.FinishBuild(ctx, grant); status.Code(e) != codes.FailedPrecondition || backend.sealCalls != 0 {
		t.Fatal("partial build became searchable", e)
	}
	// Recreate the handler; recovery must use durable receipts, not process memory.
	s = NewIndexes(New(s.chunks.pool, ops, nil, objects), m, refs, backend, nil)
	changed := proto.Clone(second).(*indexingv1.CommitBatchRequest)
	changed.Result.Data[0].Embedding[0] = -1
	if _, e := s.CommitBatch(ctx, changed); status.Code(e) != codes.FailedPrecondition || backend.bulkCalls != 2 {
		t.Fatal("new inference replaced pending vectors", e)
	}
	if _, e := s.CommitBatch(ctx, second); e != nil {
		t.Fatal(e)
	}
	objects.offline = true
	if _, e := s.CommitBatch(ctx, second); e != nil || backend.bulkCalls != 3 {
		t.Fatal("committed replay repeated bulk or required input blob", e)
	}
	objects.offline = false
	for v = currentVersion(t, s, v.ID); v.Next < v.Count; v = currentVersion(t, s, v.ID) {
		if _, e := s.CommitBatch(ctx, batchReceipt(t, s, grant, v.Next)); e != nil {
			t.Fatal(e)
		}
	}
	if _, e := s.FinishBuild(ctx, grant); status.Code(e) != codes.Unavailable {
		t.Fatal("unconfirmed seal accepted", e)
	}
	v = currentVersion(t, s, v.ID)
	if v.State != "indexing" || v.Next != v.Count || !backend.sealed {
		t.Fatal("lost seal receipt was marked ready", v)
	}
	s = NewIndexes(New(s.chunks.pool, ops, nil, objects), m, refs, backend, nil)
	if _, e := s.FinishBuild(ctx, grant); e != nil {
		t.Fatal(e)
	}
	if _, e := s.FinishBuild(ctx, grant); e != nil || backend.sealCalls != 2 {
		t.Fatal("ready replay sealed twice", e)
	}
	v = currentVersion(t, s, v.ID)
	if v.State != "ready" || len(backend.records) != v.Count || v.ReferenceState != "held" || refs.releases != 0 {
		t.Fatal("recovered result differs", v)
	}
}

func TestIndexActivationRacesVersionDeletion(t *testing.T) {
	s, m, _, _, _, _, run := indexFixture(t)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	idx, v, _ := newVersion(t, s, m, run)
	markSearchReady(t, s, v)
	current, e := s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	if e = json.Unmarshal(current.Json, &idx); e != nil {
		t.Fatal(e)
	}
	start := make(chan struct{})
	results := make(chan error, 2)
	go func() {
		<-start
		_, e := s.Activate(ctx, &indexingv1.MutationRequest{IndexId: idx.ID, IndexVersionId: v.ID, ExpectedRevision: idx.Revision})
		results <- e
	}()
	go func() {
		<-start
		_, e := s.DeleteVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: v.ID})
		results <- e
	}()
	close(start)
	wins, conflicts := 0, 0
	for range 2 {
		select {
		case e := <-results:
			if e == nil {
				wins++
			} else if status.Code(e) == codes.FailedPrecondition {
				conflicts++
			} else {
				t.Fatal(e)
			}
		case <-ctx.Done():
			t.Fatal("activation/deletion did not release locks")
		}
	}
	if wins != 1 || conflicts != 1 {
		t.Fatal("activation/deletion must have one winner", wins, conflicts)
	}
	current, e = s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	var after indexing.Index
	if e = json.Unmarshal(current.Json, &after); e != nil {
		t.Fatal(e)
	}
	v = currentVersion(t, s, v.ID)
	if after.Revision != idx.Revision+1 || !(after.ActiveVersionID == v.ID && v.State == "ready" || after.ActiveVersionID == "" && v.State == "deleting") {
		t.Fatal("active version is inconsistent with deletion", after, v.State)
	}
}

func TestSearchRerankerUnavailableThenRecovery(t *testing.T) {
	for _, phase := range []string{"profile", "scores"} {
		t.Run(phase, func(t *testing.T) {
			s, _, v, m, _ := rerankFixture(t)
			ctx := context.Background()
			req := enableSearchRerank(v, m.p)
			failure := status.Error(codes.Unavailable, "private upstream failure with candidate text")
			if phase == "profile" {
				m.profileErr = failure
			} else {
				m.err = failure
			}
			if out, e := s.Search(ctx, req); out != nil || status.Code(e) != codes.Unavailable || strings.Contains(e.Error(), "private") {
				t.Fatal("failed reranking fell back or leaked details", out, e)
			}
			wantScores := 1
			if phase == "profile" {
				wantScores = 0
			}
			if m.profiles != 1 || m.scores != wantScores {
				t.Fatal("unexpected Model retry", m.profiles, m.scores)
			}
			// Explicit new request after recovery; no hidden retry of the failed one.
			m.profileErr = nil
			m.err = nil
			out, e := s.Search(ctx, req)
			if e != nil {
				t.Fatal(e)
			}
			var response retrieval.Response
			if e = json.Unmarshal(out.Json, &response); e != nil {
				t.Fatal(e)
			}
			if !response.Rerank.Applied || len(response.Results) != 1 || m.profiles != 2 || m.scores != wantScores+1 {
				t.Fatal("recovered request differs", response)
			}
			bounded, stop := context.WithTimeout(ctx, 5*time.Second)
			defer stop()
			if _, e = s.DeleteVersion(bounded, &indexingv1.VersionRequest{IndexVersionId: v.ID}); e != nil {
				t.Fatal("failed/recovered search leaked catalog locks", e)
			}
		})
	}
}

// A failed later embedding batch must not make partial data searchable or
// replace an already active version. SQL-ready is only the baseline fixture.
func TestIndexFailedOperationPreservesProgressAndActiveVersion(t *testing.T) {
	s, m, refs, ops, _, _, run := indexFixture(t, strings.Repeat("격리된 작업 실패 확인 문장입니다. ", 600))
	m.p.MaxBatchSize = 2
	ctx := context.Background()
	idx, baseline, _ := newVersion(t, s, m, run)
	markSearchReady(t, s, baseline)
	current, e := s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	if e = json.Unmarshal(current.Json, &idx); e != nil {
		t.Fatal(e)
	}
	activated, e := s.Activate(ctx, &indexingv1.MutationRequest{IndexId: idx.ID, IndexVersionId: baseline.ID, ExpectedRevision: idx.Revision})
	if e != nil {
		t.Fatal(e)
	}
	if e = json.Unmarshal(activated.Json, &idx); e != nil {
		t.Fatal(e)
	}
	raw, e := s.CreateBuild(ctx, &indexingv1.CreateBuildRequest{IndexId: idx.ID, ChunkingRunId: run, ParentVersionId: baseline.ID, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	v, e := versionFrom(raw.Json)
	if e != nil {
		t.Fatal(e)
	}
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	grant := executionFor(v, ops)
	if _, e = s.PrepareBuild(ctx, grant); e != nil {
		t.Fatal(e)
	}
	if _, e = s.CommitBatch(ctx, batchReceipt(t, s, grant, 0)); e != nil {
		t.Fatal(e)
	}
	ops.grant.Status = "failed"
	ops.grant.ErrorCode = "MODEL_EMBEDDING_FAILED"
	ops.grant.ErrorMessage = "test model unavailable"
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	v = currentVersion(t, s, v.ID)
	if v.State != "failed" || v.Next != 2 || v.Count <= 2 || v.ErrorCode != "MODEL_EMBEDDING_FAILED" || v.ReferenceState != "held" || refs.releases != 0 {
		t.Fatal("failed build lost progress or released source", v)
	}
	if _, e = s.FinishBuild(ctx, grant); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("failed build became ready", e)
	}
	current, e = s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	var after indexing.Index
	if e = json.Unmarshal(current.Json, &after); e != nil {
		t.Fatal(e)
	}
	if after.ActiveVersionID != baseline.ID {
		t.Fatal("failed replacement changed active version", after)
	}
	if _, e = s.Activate(ctx, &indexingv1.MutationRequest{IndexId: idx.ID, IndexVersionId: v.ID, ExpectedActiveVersionId: baseline.ID, ExpectedRevision: after.Revision}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("partial failed version activated", e)
	}
}
