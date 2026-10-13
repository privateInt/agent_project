package app

import (
	"context"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
	"testing"
)

func TestHNSWLegacyReplayCustomConflictAndPersistence(t *testing.T) {
	s, m, _, _, _, _, run := indexFixture(t)
	ctx := context.Background()
	idx, v, req := newVersion(t, s, m, run)
	keyHash := chunking.Digest([]byte(req.IdempotencyKey))
	var fingerprint string
	if e := s.chunks.pool.QueryRow(ctx, `SELECT request_hash FROM api_requests WHERE scope='index.build' AND key_hash=$1`, keyHash).Scan(&fingerprint); e != nil {
		t.Fatal(e)
	}
	// This is the exact pre-HNSW request shape, including its original nil/empty fields.
	legacyHash := chunking.HashJSON(struct {
		Version, Index, Run, Parent, Deployment string
		IDs                                     []string
		New                                     newChunkRequest
	}{"index-build-v1", idx.ID, run, "", "", []string{}, newChunkRequest{}})
	if fingerprint != legacyHash {
		t.Fatal("existing replay key would conflict")
	}
	legacy := v
	legacy.IndexingConfig = nil
	raw, _ := json.Marshal(legacy)
	cached, _ := proto.Marshal(&indexingv1.JSONResponse{Json: raw})
	if _, e := s.chunks.pool.Exec(ctx, `UPDATE index_versions SET spec_json=$2 WHERE index_version_id=$1`, v.ID, raw); e != nil {
		t.Fatal(e)
	}
	if _, e := s.chunks.pool.Exec(ctx, `UPDATE api_requests SET response=$2 WHERE scope='index.build' AND key_hash=$1`, keyHash, cached); e != nil {
		t.Fatal(e)
	}
	m.offline = true
	for _, input := range []string{"", `{}`, `{"m":16}`, `{"ef_construction":128,"m":16}`} {
		req.HnswJson = []byte(input)
		out, e := s.CreateBuild(ctx, req)
		if e != nil {
			t.Fatal(e)
		}
		got, e := versionFrom(out.Json)
		if e != nil || got.ID != v.ID || got.IndexingConfig == nil || *got.IndexingConfig != indexing.DefaultConfig() || m.calls != 1 {
			t.Fatal("legacy replay changed", got, e)
		}
	}
	out, e := s.GetVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: v.ID})
	if e != nil {
		t.Fatal(e)
	}
	got, e := versionFrom(out.Json)
	if e != nil || *got.IndexingConfig != indexing.DefaultConfig() {
		t.Fatal("legacy read did not resolve defaults")
	}
	req.HnswJson = []byte(`{"m":32}`)
	if _, e = s.CreateBuild(ctx, req); status.Code(e) != codes.AlreadyExists || m.calls != 1 {
		t.Fatal("different HNSW reused old key", e)
	}
	m.offline = false
	req.IdempotencyKey = uuid.NewString()
	req.HnswJson = []byte(`{"m":32,"ef_construction":256}`)
	out, e = s.CreateBuild(ctx, req)
	if e != nil {
		t.Fatal(e)
	}
	custom, e := versionFrom(out.Json)
	if e != nil || custom.IndexingConfig.Parameters != (indexing.HNSW{M: 32, EFConstruction: 256}) {
		t.Fatal(custom, e)
	}
	out, e = s.GetVersion(ctx, &indexingv1.VersionRequest{IndexVersionId: custom.ID})
	if e != nil {
		t.Fatal(e)
	}
	stored, e := versionFrom(out.Json)
	if e != nil || *stored.IndexingConfig != *custom.IndexingConfig {
		t.Fatal("config not persisted", e)
	}
	calls := m.calls
	req.HnswJson = []byte(`{"ef_construction":256,"m":32}`)
	if _, e = s.CreateBuild(ctx, req); e != nil || m.calls != calls {
		t.Fatal("normalized retry reran model", e)
	}
	req.HnswJson = []byte(`{"m":48,"ef_construction":256}`)
	if _, e = s.CreateBuild(ctx, req); status.Code(e) != codes.AlreadyExists || m.calls != calls {
		t.Fatal("custom conflict accepted", e)
	}
	if _, e = s.chunks.pool.Exec(ctx, `UPDATE index_versions SET state='ready' WHERE index_version_id=$1`, custom.ID); e != nil {
		t.Fatal(e)
	}
	out, e = s.CreateBuild(ctx, &indexingv1.CreateBuildRequest{IndexId: idx.ID, ChunkingRunId: run, ParentVersionId: custom.ID, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	child, e := versionFrom(out.Json)
	if e != nil || *child.IndexingConfig != indexing.DefaultConfig() {
		t.Fatal("omitted settings implicitly inherited parent", e)
	}
	current, e := s.GetIndex(ctx, &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	var i indexing.Index
	_ = json.Unmarshal(current.Json, &i)
	if i.ActiveVersionID != "" {
		t.Fatal("build auto-activated a version")
	}
}
func TestHNSWInvalidDirectRPCDoesNotCreateOrInvokeModel(t *testing.T) {
	s, m, _, _, _, _, run := indexFixture(t)
	ctx := context.Background()
	idx, _, req := newVersion(t, s, m, run)
	calls := m.calls
	for _, raw := range []string{`null`, `{"m":0}`, `{"m":65}`, `{"ef_construction":1025}`, `{"m":null}`, `{"m":true}`, `{"m":16.0}`, `{"m":16,"m":32}`, `{"ef_search":20}`} {
		req.IdempotencyKey = uuid.NewString()
		req.HnswJson = []byte(raw)
		if _, e := s.CreateBuild(ctx, req); status.Code(e) != codes.InvalidArgument {
			t.Fatal(raw, e)
		}
	}
	var count int
	if e := s.chunks.pool.QueryRow(ctx, `SELECT count(*) FROM index_versions WHERE index_id=$1`, idx.ID).Scan(&count); e != nil || count != 1 || m.calls != calls {
		t.Fatal("invalid settings had side effects", count, m.calls, e)
	}
}
