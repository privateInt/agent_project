package app

import (
	"context"
	"google.golang.org/grpc"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	"net/http/httptest"
	"strings"
	"testing"
)

type hnswClient struct {
	indexingv1.IndexingServiceClient
	calls   int
	request *indexingv1.CreateBuildRequest
}

func (c *hnswClient) CreateBuild(_ context.Context, r *indexingv1.CreateBuildRequest, _ ...grpc.CallOption) (*indexingv1.JSONResponse, error) {
	c.calls++
	c.request = r
	return &indexingv1.JSONResponse{Json: []byte(`{}`)}, nil
}
func TestBuildRESTHNSWValidationAndForwarding(t *testing.T) {
	c := &hnswClient{}
	handler := (&Server{Indexes: c}).Handler()
	call := func(body string) int {
		w := httptest.NewRecorder()
		r := httptest.NewRequest("POST", "/api/indexes/test/builds", strings.NewReader(body))
		r.Header.Set("Idempotency-Key", "hnsw-replay")
		handler.ServeHTTP(w, r)
		return w.Code
	}
	for _, raw := range []string{`{}`, `{"chunking_run_id":"run","hnsw":{"m":32,"ef_construction":256}}`} {
		if got := call(raw); got != 202 || c.request.IdempotencyKey != "hnsw-replay" {
			t.Fatal(got)
		}
	}
	if string(c.request.HnswJson) != `{"m":32,"ef_construction":256}` {
		t.Fatal("HNSW lost at REST/gRPC boundary")
	}
	for _, raw := range []string{`{"hnsw":null}`, `{"hnsw":{"m":0}}`, `{"hnsw":{"m":32.5}}`, `{"hnsw":{"m":null}}`, `{"hnsw":{"m":16,"m":32}}`, `{"hnsw":{"ef_search":20}}`, `{"hnsw":{"ef_construction":1025}}`, `{"indexing_config":{}}`} {
		if got := call(raw); got != 400 {
			t.Fatal("bad request forwarded", raw, got)
		}
	}
	if c.calls != 2 {
		t.Fatal("invalid request reached service")
	}
}
