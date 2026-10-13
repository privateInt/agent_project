package app

import (
	collectionv1 "data-platform/gen/collection/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"io"
	"net/http"
)

func deleteTarget(w http.ResponseWriter, r *http.Request) (string, bool) {
	id := r.PathValue("id")
	parsed, e := uuid.Parse(id)
	// No flags/body/query may accidentally broaden a single-resource removal.
	body, readErr := io.ReadAll(http.MaxBytesReader(w, r.Body, 1))
	if e != nil || parsed == uuid.Nil || parsed.String() != id || r.URL.RawQuery != "" || readErr != nil || len(body) != 0 {
		failure(w, status.Error(codes.InvalidArgument, "DELETE requires one canonical UUID and no query or body"))
		return "", false
	}
	return id, true
}

func (s *Server) deleteDocument(w http.ResponseWriter, r *http.Request) {
	id, ok := deleteTarget(w, r)
	if !ok {
		return
	}
	if s.Collection == nil {
		failure(w, status.Error(codes.Unavailable, "Collection service unavailable"))
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	result, e := s.Collection.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: id})
	if e != nil {
		failure(w, e)
		return
	}
	if result.GetDocumentId() != id || result.GetDeletedAt() == nil || result.GetDeletedAt().CheckValid() != nil {
		failure(w, status.Error(codes.DataLoss, "invalid deletion response"))
		return
	}
	write(w, 200, documentView(result))
}

func (s *Server) deleteRefinement(w http.ResponseWriter, r *http.Request) {
	id, ok := deleteTarget(w, r)
	if !ok || !s.refinementReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	result, e := s.Refinement.DeleteRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: id})
	if e != nil {
		failure(w, e)
		return
	}
	if result.GetRefinementRunId() != id || result.GetDeletedAt() == nil || result.GetDeletedAt().CheckValid() != nil {
		failure(w, status.Error(codes.DataLoss, "invalid deletion response"))
		return
	}
	write(w, 200, refinementView(result))
}
