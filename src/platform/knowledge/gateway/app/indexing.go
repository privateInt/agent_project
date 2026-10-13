package app

import (
	"context"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"io"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
	"net/http"
)

func indexBody(w http.ResponseWriter, r *http.Request, v any) error {
	raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 64<<10))
	if e != nil {
		return status.Error(codes.ResourceExhausted, "request exceeds 64 KiB")
	}
	if chunking.Decode(raw, v) != nil {
		return status.Error(codes.InvalidArgument, "invalid or unknown request fields")
	}
	return nil
}
func (s *Server) indexRoutes(mux *http.ServeMux) {
	mux.HandleFunc("POST /api/indexes/{id}/search", s.search)
	reply := func(w http.ResponseWriter, out *indexingv1.JSONResponse, e error, code int) {
		if e != nil {
			fail(w, e)
			return
		}
		writeRaw(w, code, out.Json)
	}
	mux.HandleFunc("POST /api/indexes", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Name         string `json:"name"`
			DeploymentID string `json:"embedding_deployment_id"`
		}
		if e := indexBody(w, r, &b); e != nil {
			fail(w, e)
			return
		}
		out, e := s.Indexes.CreateIndex(r.Context(), &indexingv1.CreateIndexRequest{Name: b.Name, EmbeddingDeploymentId: b.DeploymentID, IdempotencyKey: r.Header.Get("Idempotency-Key")})
		reply(w, out, e, 201)
	})
	mux.HandleFunc("GET /api/indexes", func(w http.ResponseWriter, r *http.Request) {
		n, e := limit(r)
		if e != nil {
			fail(w, e)
			return
		}
		out, e := s.Indexes.ListIndexes(r.Context(), &indexingv1.ListRequest{Limit: n, Cursor: r.URL.Query().Get("cursor")})
		reply(w, out, e, 200)
	})
	mux.HandleFunc("GET /api/indexes/{id}", func(w http.ResponseWriter, r *http.Request) {
		out, e := s.Indexes.GetIndex(r.Context(), &indexingv1.IndexRequest{IndexId: r.PathValue("id")})
		reply(w, out, e, 200)
	})
	mux.HandleFunc("GET /api/indexes/{id}/versions", func(w http.ResponseWriter, r *http.Request) {
		n, e := limit(r)
		if e != nil {
			fail(w, e)
			return
		}
		out, e := s.Indexes.ListVersions(r.Context(), &indexingv1.ListRequest{IndexId: r.PathValue("id"), Limit: n, Cursor: r.URL.Query().Get("cursor")})
		reply(w, out, e, 200)
	})
	mux.HandleFunc("GET /api/index-versions/{id}", func(w http.ResponseWriter, r *http.Request) {
		out, e := s.Indexes.GetVersion(r.Context(), &indexingv1.VersionRequest{IndexVersionId: r.PathValue("id")})
		reply(w, out, e, 200)
	})
	mux.HandleFunc("POST /api/indexes/{id}/builds", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			RunID        string          `json:"chunking_run_id"`
			Chunking     json.RawMessage `json:"chunking"`
			CanonicalIDs []string        `json:"canonical_ids"`
			ParentID     string          `json:"parent_version_id"`
			DeploymentID string          `json:"embedding_deployment_id"`
			HNSW         json.RawMessage `json:"hnsw"`
		}
		if e := indexBody(w, r, &b); e != nil {
			fail(w, e)
			return
		}
		if _, e := indexing.ParseHNSW(b.HNSW); e != nil {
			fail(w, status.Error(codes.InvalidArgument, e.Error()))
			return
		}
		out, e := s.Indexes.CreateBuild(r.Context(), &indexingv1.CreateBuildRequest{IndexId: r.PathValue("id"), ChunkingRunId: b.RunID, ChunkingRequestJson: b.Chunking, CanonicalIds: b.CanonicalIDs, ParentVersionId: b.ParentID, EmbeddingDeploymentId: b.DeploymentID, HnswJson: b.HNSW, IdempotencyKey: r.Header.Get("Idempotency-Key")})
		reply(w, out, e, 202)
	})
	mutate := func(fn func(context.Context, *indexingv1.MutationRequest) (*indexingv1.JSONResponse, error), code int) http.HandlerFunc {
		return func(w http.ResponseWriter, r *http.Request) {
			var b struct {
				VersionID string `json:"index_version_id"`
				Expected  string `json:"expected_active_version_id"`
				Revision  int64  `json:"expected_revision"`
			}
			if e := indexBody(w, r, &b); e != nil {
				fail(w, e)
				return
			}
			out, e := fn(r.Context(), &indexingv1.MutationRequest{IndexId: r.PathValue("id"), IndexVersionId: b.VersionID, ExpectedActiveVersionId: b.Expected, ExpectedRevision: b.Revision})
			reply(w, out, e, code)
		}
	}
	mux.HandleFunc("POST /api/indexes/{id}/activate", mutate(func(c context.Context, r *indexingv1.MutationRequest) (*indexingv1.JSONResponse, error) {
		return s.Indexes.Activate(c, r)
	}, 200))
	mux.HandleFunc("DELETE /api/indexes/{id}", mutate(func(c context.Context, r *indexingv1.MutationRequest) (*indexingv1.JSONResponse, error) {
		return s.Indexes.DeleteIndex(c, r)
	}, 202))
	mux.HandleFunc("DELETE /api/index-versions/{id}", func(w http.ResponseWriter, r *http.Request) {
		out, e := s.Indexes.DeleteVersion(r.Context(), &indexingv1.VersionRequest{IndexVersionId: r.PathValue("id")})
		reply(w, out, e, 202)
	})
}
