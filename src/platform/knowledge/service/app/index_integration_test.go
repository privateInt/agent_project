package app

import (
	"context"
	"encoding/json"
	"fmt"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	chunkingv1 "knowledge-platform/gen/chunking/v1"
	refinementv1 "knowledge-platform/gen/data/refinement/v1"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/testsupport"
	"strings"
	"testing"
)

type indexModel struct {
	embeddingv1.EmbeddingServiceClient
	p       *embeddingv1.EmbeddingProfile
	calls   int
	offline bool
}

func (m *indexModel) GetProfile(context.Context, *embeddingv1.GetProfileRequest, ...grpc.CallOption) (*embeddingv1.EmbeddingProfile, error) {
	m.calls++
	if m.offline {
		return nil, unavailable("offline")
	}
	return proto.Clone(m.p).(*embeddingv1.EmbeddingProfile), nil
}

type indexRefs struct {
	refinementv1.RefinementServiceClient
	releases    int
	failRelease bool
}

func (f *indexRefs) AcquireKnowledgeReferences(_ context.Context, r *refinementv1.KnowledgeReferencesRequest, _ ...grpc.CallOption) (*refinementv1.KnowledgeReferences, error) {
	return &refinementv1.KnowledgeReferences{IndexVersionId: r.IndexVersionId, State: "held", Fingerprint: chunking.HashJSON(r.Documents), Documents: r.Documents}, nil
}
func (f *indexRefs) ReleaseKnowledgeReferences(_ context.Context, r *refinementv1.KnowledgeReferenceOwner, _ ...grpc.CallOption) (*refinementv1.KnowledgeReferences, error) {
	f.releases++
	if f.failRelease {
		return nil, unavailable("offline")
	}
	return &refinementv1.KnowledgeReferences{IndexVersionId: r.IndexVersionId, State: "released"}, nil
}

type indexOps struct {
	operationv1.OperationServiceClient
	grant *operationv1.Operation
}

func (f *indexOps) GetOperation(context.Context, *operationv1.GetOperationRequest, ...grpc.CallOption) (*operationv1.Operation, error) {
	return f.grant, nil
}

type indexObjects struct {
	body    []byte
	offline bool
}

func (f *indexObjects) Open(context.Context, string) (io.ReadCloser, error) {
	if f.offline {
		return nil, fmt.Errorf("offline")
	}
	return io.NopCloser(strings.NewReader(string(f.body))), nil
}
func (f *indexObjects) Put(context.Context, string, io.Reader, int64, string) error { return nil }

type indexBackend struct {
	bulkCalls, sealCalls, deleteCalls int
	failBulk, failDelete              bool
	config                            indexing.Config
}

func (f *indexBackend) Ensure(_ context.Context, v indexing.Version) error {
	c, e := v.EffectiveConfig()
	f.config = c
	return e
}
func (f *indexBackend) Bulk(context.Context, indexing.Version, []indexing.Record) error {
	f.bulkCalls++
	if f.failBulk {
		return fmt.Errorf("bulk reply lost")
	}
	return nil
}
func (f *indexBackend) Seal(context.Context, indexing.Version) error { f.sealCalls++; return nil }
func (f *indexBackend) Delete(context.Context, indexing.Version) error {
	f.deleteCalls++
	if f.failDelete {
		return fmt.Errorf("delete reply lost")
	}
	return nil
}
func indexFixture(t *testing.T, text ...string) (*IndexService, *indexModel, *indexRefs, *indexOps, *indexBackend, *indexObjects, string) {
	t.Helper()
	pool := testsupport.Postgres(t)
	ctx := context.Background()
	if e := InitSchema(ctx, pool); e != nil {
		t.Fatal(e)
	}
	deployment := uuid.NewString()
	model := &indexModel{p: &embeddingv1.EmbeddingProfile{DeploymentId: deployment, ModelVersionId: uuid.NewString(), ProfileId: "snowflake-ko-v1", EmbeddingSpaceId: "es_" + strings.Repeat("a", 64), SourceManifestSha256: strings.Repeat("b", 64), TokenizerSha256: strings.Repeat("c", 64), RuntimeImageId: "sha256:" + strings.Repeat("d", 64), RuntimeContainerId: strings.Repeat("e", 64), ServedModelName: "deployment-" + deployment, RepoId: "dragonkue/snowflake-arctic-embed-l-v2.0-ko", QueryPrefix: "query: ", Pooling: "cls", Normalization: "l2", Dimension: 1024, MaxInputTokens: 2048, MaxBatchSize: 16, MaxInputBytes: 131072, Truncation: "reject", Capability: "embedding", Status: "ready"}}
	refs := &indexRefs{}
	ops := &indexOps{}
	backend := &indexBackend{}
	objects := &indexObjects{}
	service := New(pool, ops, nil, objects)
	s := NewIndexes(service, model, refs, backend, nil)
	source, canonical, runID := uuid.NewString(), uuid.NewString(), uuid.NewString()
	paragraph := "대한민국의 수도는 서울입니다."
	if len(text) > 0 {
		paragraph = text[0]
	}
	doc := map[string]any{"schema_version": "canonical-document-v1", "source": map[string]any{"document_id": source, "sha256": strings.Repeat("a", 64), "format": "text"}, "processor": map[string]any{"version": "cpu-parser-v1", "rules_version": "basic-cleaning-v1", "image_id": "sha256:" + strings.Repeat("b", 64)}, "blocks": []any{map[string]any{"id": "b1", "order": 0, "type": "paragraph", "text": paragraph, "location": map[string]any{}}}, "pages": []any{}, "quality": map[string]any{"state": "ready", "issues": []any{}}}
	raw, _ := json.Marshal(doc)
	input := chunking.Input{CanonicalID: canonical, CanonicalSHA256: chunking.Digest(raw), SizeBytes: int64(len(raw)), SourceDocumentID: source, SourceSHA256: strings.Repeat("a", 64), RefinementRunID: uuid.NewString(), SchemaVersion: "canonical-document-v1", ProcessorVersion: "cpu-parser-v1", RulesVersion: "basic-cleaning-v1", ProcessorImageID: "sha256:" + strings.Repeat("b", 64), Format: "text"}
	selection, _ := chunking.Normalize("structure_v1", nil)
	spec, e := chunking.NewSpec([]chunking.Input{input}, selection)
	if e != nil {
		t.Fatal(e)
	}
	result, e := chunking.Build(spec, map[string][]byte{canonical: raw})
	if e != nil {
		t.Fatal(e)
	}
	objects.body, _ = json.Marshal(result)
	specRaw, _ := json.Marshal(spec)
	if _, e = pool.Exec(ctx, `INSERT INTO chunking_runs(chunking_run_id,operation_id,name,spec_json,result_sha256,result_size_bytes,chunk_count) VALUES($1,$2,'test',$3,$4,$5,$6)`, runID, uuid.NewString(), specRaw, chunking.Digest(objects.body), len(objects.body), len(result.Chunks)); e != nil {
		t.Fatal(e)
	}
	return s, model, refs, ops, backend, objects, runID
}
func newVersion(t *testing.T, s *IndexService, m *indexModel, runID string, hnsw ...string) (indexing.Index, indexing.Version, *indexingv1.CreateBuildRequest) {
	t.Helper()
	ctx := context.Background()
	raw, e := s.CreateIndex(ctx, &indexingv1.CreateIndexRequest{Name: "test", EmbeddingDeploymentId: m.p.DeploymentId, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	var idx indexing.Index
	_ = json.Unmarshal(raw.Json, &idx)
	req := &indexingv1.CreateBuildRequest{IndexId: idx.ID, ChunkingRunId: runID, IdempotencyKey: uuid.NewString()}
	if len(hnsw) > 0 {
		req.HnswJson = []byte(hnsw[0])
	}
	raw, e = s.CreateBuild(ctx, req)
	if e != nil {
		t.Fatal(e)
	}
	v, e := versionFrom(raw.Json)
	if e != nil {
		t.Fatal(e)
	}
	return idx, v, req
}
func TestIndexBuildReplayBatchesSealActivationAndCleanup(t *testing.T) {
	s, m, refs, ops, backend, objects, runID := indexFixture(t)
	ctx := context.Background()
	idx, v, create := newVersion(t, s, m, runID, `{"m":32,"ef_construction":256}`)
	m.offline = true
	again, e := s.CreateBuild(ctx, create)
	if e != nil || m.calls != 1 {
		t.Fatal("replay reread Model", e)
	}
	replayed, _ := versionFrom(again.Json)
	if replayed.ID != v.ID {
		t.Fatal("replay changed version")
	}
	m.offline = false
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	token, instance := uuid.NewString(), uuid.NewString()
	grant := &indexingv1.ExecutionRequest{IndexVersionId: v.ID, OperationId: v.OperationID, DispatchToken: token, ExecutionInstanceId: instance}
	ops.grant = &operationv1.Operation{OperationId: v.OperationID, ResourceId: v.ID, ResourceType: "index_version", ExecutionType: "indexing.build", DispatchProtocol: 1, DispatchToken: token, ExecutionInstanceId: instance, Status: "running", ExecutionStartedAt: timestamppb.Now()}
	stale := proto.Clone(grant).(*indexingv1.ExecutionRequest)
	stale.DispatchToken = uuid.NewString()
	if _, e = s.PrepareBuild(ctx, stale); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("stale writer accepted", e)
	}
	if _, e = s.PrepareBuild(ctx, grant); e != nil {
		t.Fatal(e)
	}
	if backend.config.Parameters != (indexing.HNSW{M: 32, EFConstruction: 256}) {
		t.Fatal("custom config lost during execution", backend.config)
	}
	if _, e = s.FinishBuild(ctx, grant); status.Code(e) != codes.FailedPrecondition || backend.sealCalls != 0 {
		t.Fatal("partial build sealed", e)
	}
	raw, e := s.ReadBatch(ctx, &indexingv1.ReadBatchRequest{Execution: grant})
	if e != nil {
		t.Fatal(e)
	}
	var batch indexing.Batch
	_ = json.Unmarshal(raw.Json, &batch)
	vector := make([]float64, 1024)
	vector[0] = 1
	result := &embeddingv1.EmbedResponse{Profile: batch.Version.Profile, Role: "document", Data: []*embeddingv1.EmbeddingOutput{{Id: batch.Chunks[0].ID, Embedding: vector, InputTokens: 10}}}
	request := &indexingv1.CommitBatchRequest{Execution: grant, Result: result}
	backend.failBulk = true
	if _, e = s.CommitBatch(ctx, request); status.Code(e) != codes.Unavailable {
		t.Fatal(e)
	}
	changed := proto.Clone(request).(*indexingv1.CommitBatchRequest)
	changed.Result.Data[0].Embedding[0] = -1
	if _, e = s.CommitBatch(ctx, changed); status.Code(e) != codes.FailedPrecondition || backend.bulkCalls != 1 {
		t.Fatal("new inference replaced pending receipt", e)
	}
	backend.failBulk = false
	if _, e = s.CommitBatch(ctx, request); e != nil {
		t.Fatal(e)
	}
	objects.offline = true
	if _, e = s.CommitBatch(ctx, request); e != nil || backend.bulkCalls != 2 {
		t.Fatal("committed replay required blob or wrote again", e)
	}
	objects.offline = false
	if _, e = s.FinishBuild(ctx, grant); e != nil {
		t.Fatal(e)
	}
	if _, e = s.FinishBuild(ctx, grant); e != nil || backend.sealCalls != 1 {
		t.Fatal("ready receipt repeated side effect", e)
	}
	current, e := s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	_ = json.Unmarshal(current.Json, &idx)
	activate := &indexingv1.MutationRequest{IndexId: idx.ID, IndexVersionId: v.ID, ExpectedRevision: idx.Revision}
	if _, e = s.Activate(ctx, activate); e != nil {
		t.Fatal(e)
	}
	if _, e = s.Activate(ctx, activate); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("stale activation accepted", e)
	}
	if _, e = s.DeleteVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: v.ID}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("active version deleted", e)
	}
	current, _ = s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	_ = json.Unmarshal(current.Json, &idx)
	if _, e = s.DeleteIndex(ctx, &indexingv1.MutationRequest{IndexId: idx.ID, ExpectedRevision: idx.Revision, ExpectedActiveVersionId: v.ID}); e != nil {
		t.Fatal(e)
	}
	backend.failDelete = true
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	if refs.releases != 0 {
		t.Fatal("Data released before physical deletion")
	}
	backend.failDelete = false
	refs.failRelease = true
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	out, _ := s.GetVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: v.ID})
	pending, _ := versionFrom(out.Json)
	if pending.State != "deleting" {
		t.Fatal("unconfirmed references marked deleted")
	}
	refs.failRelease = false
	if e = s.maintainVersion(ctx, v.ID); e != nil {
		t.Fatal(e)
	}
	out, _ = s.GetVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: v.ID})
	done, _ := versionFrom(out.Json)
	if done.State != "deleted" || done.ReferenceState != "released" {
		t.Fatal(done)
	}
	if _, e = s.PrepareBuild(ctx, grant); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("deleted index resurrected", e)
	}
	run, e := s.chunks.GetRun(ctx, &chunkingv1.GetRunRequest{ChunkingRunId: runID})
	if e != nil || run.ResultSha256 == "" {
		t.Fatal("shared ChunkSet deleted", e)
	}
}
func TestConcurrentActivationHasOneWinnerAndFailedBuildPreservesWinner(t *testing.T) {
	s, m, _, _, _, _, runID := indexFixture(t)
	ctx := context.Background()
	idx, a, _ := newVersion(t, s, m, runID)
	raw, e := s.CreateBuild(ctx, &indexingv1.CreateBuildRequest{IndexId: idx.ID, ChunkingRunId: runID, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	b, _ := versionFrom(raw.Json)
	if _, e = s.chunks.pool.Exec(ctx, `UPDATE index_versions SET state='ready',reference_state='held',chunk_count=1,indexed_count=1 WHERE index_id=$1`, idx.ID); e != nil {
		t.Fatal(e)
	}
	current, _ := s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	_ = json.Unmarshal(current.Json, &idx)
	done := make(chan error, 2)
	for _, v := range []indexing.Version{a, b} {
		go func(id string) {
			_, e := s.Activate(ctx, &indexingv1.MutationRequest{IndexId: idx.ID, IndexVersionId: id, ExpectedRevision: idx.Revision})
			done <- e
		}(v.ID)
	}
	passed, conflict := 0, 0
	for range 2 {
		e := <-done
		if e == nil {
			passed++
		} else if status.Code(e) == codes.FailedPrecondition {
			conflict++
		} else {
			t.Fatal(e)
		}
	}
	if passed != 1 || conflict != 1 {
		t.Fatal(passed, conflict)
	}
	current, _ = s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	var winner indexing.Index
	_ = json.Unmarshal(current.Json, &winner)
	raw, e = s.CreateBuild(ctx, &indexingv1.CreateBuildRequest{IndexId: idx.ID, ChunkingRunId: runID, ParentVersionId: winner.ActiveVersionID, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	failed, _ := versionFrom(raw.Json)
	if _, e = s.chunks.pool.Exec(ctx, `UPDATE index_versions SET state='failed' WHERE index_version_id=$1`, failed.ID); e != nil {
		t.Fatal(e)
	}
	current, _ = s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	var after indexing.Index
	_ = json.Unmarshal(current.Json, &after)
	if after.ActiveVersionID != winner.ActiveVersionID {
		t.Fatal("failed build replaced active version")
	}
}
