package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"net/http"
	"time"
)

func (s *Server) purgeDocument(w http.ResponseWriter, r *http.Request) {
	id, ok := deleteTarget(w, r)
	if !ok {
		return
	}
	if s.Collection == nil {
		failure(w, status.Error(codes.Unavailable, "Collection service unavailable"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	result, e := s.Collection.PurgeDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: id})
	if e != nil {
		failure(w, e)
		return
	}
	if result.GetDocumentId() != id || result.GetPurgeState() != "purged" || result.GetPurgedAt() == nil || result.GetPurgedAt().CheckValid() != nil {
		failure(w, status.Error(codes.DataLoss, "invalid deletion response"))
		return
	}
	write(w, 200, documentView(result))
}

func (s *Server) purgeRefinement(w http.ResponseWriter, r *http.Request) {
	id, ok := deleteTarget(w, r)
	if !ok || !s.refinementReady(w) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	result, e := s.Refinement.PurgeRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: id})
	if e != nil {
		failure(w, e)
		return
	}
	if result.GetRefinementRunId() != id || result.GetPurgeState() != "purged" || result.GetPurgedAt() == nil || result.GetPurgedAt().CheckValid() != nil {
		failure(w, status.Error(codes.DataLoss, "invalid deletion response"))
		return
	}
	write(w, 200, refinementView(result))
}
