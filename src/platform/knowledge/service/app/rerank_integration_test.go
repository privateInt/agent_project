package app

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
	"knowledge-platform/internal/testsupport"
)

type searchReranker struct {
	rerankingv1.RerankingServiceClient
	p                *rerankingv1.RerankerProfile
	profiles, scores int
	err              error
	profileErr       error
	mutate           func(*rerankingv1.RerankResponse)
	block            func(context.Context)
}

func (m *searchReranker) GetProfile(context.Context, *rerankingv1.GetProfileRequest, ...grpc.CallOption) (*rerankingv1.RerankerProfile, error) {
	m.profiles++
	if m.profileErr != nil {
		return nil, m.profileErr
	}
	return proto.Clone(m.p).(*rerankingv1.RerankerProfile), nil
}
func (m *searchReranker) Rerank(ctx context.Context, r *rerankingv1.RerankRequest, _ ...grpc.CallOption) (*rerankingv1.RerankResponse, error) {
	m.scores++
	if m.block != nil {
		m.block(ctx)
		return nil, ctx.Err()
	}
	if m.err != nil {
		return nil, m.err
	}
	out := &rerankingv1.RerankResponse{Profile: proto.Clone(m.p).(*rerankingv1.RerankerProfile)}
	for i, c := range r.Candidates {
		out.Data = append(out.Data, &rerankingv1.Score{Id: c.Id, Index: int32(i), Score: float64(i), InputTokens: 18})
	}
	if m.mutate != nil {
		m.mutate(out)
	}
	return out, nil
}
func enableSearchRerank(v indexing.Version, p *rerankingv1.RerankerProfile) *indexingv1.SearchRequest {
	r := searchRequest(v)
	var b map[string]any
	_ = json.Unmarshal(r.QueryJson, &b)
	b["query_text"] = "환경 영향은?"
	b["top_k"] = 1
	b["candidate_k"] = 2
	b["rerank"] = map[string]any{"enabled": true, "deployment_id": p.DeploymentId, "expected_reranker_profile_id": p.RerankerProfileId}
	r.QueryJson, _ = json.Marshal(b)
	return r
}
func rerankFixture(t *testing.T) (*IndexService, indexing.Index, indexing.Version, *searchReranker, *retrieval.Candidates) {
	s, m, _, _, backend, _, run := indexFixture(t)
	idx, v, _ := newVersion(t, s, m, run)
	markSearchReady(t, s, v)
	a := testsupport.SearchChunk(v, 0)
	b := a
	b.ID = strings.Repeat("9", 64)
	candidates := &retrieval.Candidates{Hits: []retrieval.Hit{{Chunk: a, Score: .8}, {Chunk: b, Score: .2}}}
	s.search = &readBackend{indexBackend: backend, searchFn: func(context.Context, indexing.Version, retrieval.Query) (retrieval.Candidates, error) {
		return *candidates, nil
	}}
	r := &searchReranker{p: testsupport.RerankerProfile()}
	s.reranker = r
	return s, idx, v, r, candidates
}
func TestSearchRerankOffOnEmptyAndFailures(t *testing.T) {
	s, _, v, m, candidates := rerankFixture(t)
	ctx := context.Background()
	if _, e := s.Search(ctx, searchRequest(v)); e != nil || m.profiles != 0 || m.scores != 0 {
		t.Fatal("off contacted reranker", e)
	}
	req := enableSearchRerank(v, m.p)
	response, e := s.Search(ctx, req)
	if e != nil {
		t.Fatal(e)
	}
	var out retrieval.Response
	_ = json.Unmarshal(response.Json, &out)
	if !out.Rerank.Applied || out.Results[0].RetrievalRank != 2 || m.profiles != 1 || m.scores != 1 {
		t.Fatal("on did not reorder once", out)
	}
	saved := candidates.Hits
	candidates.Hits = nil
	response, e = s.Search(ctx, req)
	if e != nil {
		t.Fatal(e)
	}
	_ = json.Unmarshal(response.Json, &out)
	if out.Rerank.Applied || out.Rerank.Status != "skipped_no_candidates" || m.scores != 1 || m.profiles != 2 {
		t.Fatal("empty inferred or skipped profile", out)
	}
	m.p.RerankerProfileId = "rp_" + strings.Repeat("f", 64)
	if _, e = s.Search(ctx, req); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("empty accepted wrong profile", e)
	}
	m.p = testsupport.RerankerProfile()
	candidates.Hits = saved
	m.err = status.Error(codes.InvalidArgument, "RERANK_INPUT_TOO_LONG: private candidate text")
	if response, e = s.Search(ctx, req); response != nil || status.Code(e) != codes.InvalidArgument || strings.Contains(e.Error(), "private") {
		t.Fatal("length error leaked or fell back", response, e)
	}
	m.err = nil
	m.mutate = func(r *rerankingv1.RerankResponse) { r.Data = r.Data[:1] }
	if response, e = s.Search(ctx, req); response != nil || status.Code(e) != codes.DataLoss {
		t.Fatal("missing scores succeeded", response, e)
	}
}
func TestSearchRerankCancellationAndDeleteLock(t *testing.T) {
	s, idx, v, m, _ := rerankFixture(t)
	entered := make(chan struct{})
	m.block = func(ctx context.Context) { close(entered); <-ctx.Done() }
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan error, 1)
	go func() { _, e := s.Search(ctx, enableSearchRerank(v, m.p)); done <- e }()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("Model rerank not started")
	}
	current, e := s.GetIndex(context.Background(), &indexingv1.IndexRequest{IndexId: idx.ID})
	if e != nil {
		t.Fatal(e)
	}
	_ = json.Unmarshal(current.Json, &idx)
	bounded, stop := context.WithTimeout(context.Background(), 100*time.Millisecond)
	_, e = s.DeleteIndex(bounded, &indexingv1.MutationRequest{IndexId: idx.ID, ExpectedRevision: idx.Revision})
	stop()
	if e == nil {
		t.Fatal("delete bypassed rerank read lock")
	}
	cancel()
	if e = <-done; status.Code(e) != codes.Canceled {
		t.Fatal("cancellation lost", e)
	}
	bounded, stop = context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if _, e = s.DeleteIndex(bounded, &indexingv1.MutationRequest{IndexId: idx.ID, ExpectedRevision: idx.Revision}); e != nil {
		t.Fatal("rerank leaked locks", e)
	}
}
func TestSearchRerankDeadline(t *testing.T) {
	s, _, v, m, _ := rerankFixture(t)
	m.block = func(ctx context.Context) { <-ctx.Done() }
	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	if out, e := s.Search(ctx, enableSearchRerank(v, m.p)); out != nil || status.Code(e) != codes.DeadlineExceeded {
		t.Fatal("deadline not propagated", out, e)
	}
}
