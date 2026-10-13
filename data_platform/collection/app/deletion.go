package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/idempotency"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// DeleteDocument removes a list entry. Immutable source bytes, crawl history,
// existing jobs and refinement provenance remain readable by their original IDs.
// The first deletion timestamp is stable across concurrent calls and retries.
func (s *Service) DeleteDocument(ctx context.Context, r *collectionv1.GetDocumentRequest) (*collectionv1.SourceDocument, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	if r.GetHttpResponse() {
		return nil, status.Error(codes.InvalidArgument, "delete targets the document, not a representation")
	}
	result, e := s.pool.Exec(ctx, `UPDATE source_documents SET deleted_at=COALESCE(deleted_at,clock_timestamp()) WHERE document_id=$1`, r.DocumentId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if result.RowsAffected() == 0 {
		return nil, status.Error(codes.NotFound, "source document not found")
	}
	return s.GetDocument(ctx, r)
}
