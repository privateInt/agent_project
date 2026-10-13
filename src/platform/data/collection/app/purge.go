package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/idempotency"
	"data-platform/internal/purge"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func (s *Service) SetRefinement(c refinementv1.RefinementServiceClient) { s.refinement = c }
func (s *Service) PurgeDocument(ctx context.Context, r *collectionv1.GetDocumentRequest) (*collectionv1.SourceDocument, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	if r.GetHttpResponse() {
		return nil, status.Error(codes.InvalidArgument, "purge targets the complete source")
	}
	unlock, e := purge.Lock(ctx, s.pool, r.DocumentId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer unlock()
	doc, e := s.GetDocument(ctx, r)
	if e != nil {
		return nil, e
	}
	if doc.PurgedAt != nil {
		return doc, nil
	}
	if s.refinement == nil {
		return nil, status.Error(codes.Unavailable, "refinement reference check unavailable")
	}
	if doc.CrawlRunId != "" {
		var operationID string
		if e = s.pool.QueryRow(ctx, `SELECT operation_id::text FROM crawl_runs WHERE crawl_run_id=$1`, doc.CrawlRunId).Scan(&operationID); e != nil {
			return nil, readError(e)
		}
		if s.ops == nil {
			return nil, status.Error(codes.Unavailable, "operation service unavailable")
		}
		op, e := s.ops.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: operationID})
		if e != nil {
			return nil, e
		}
		if op.GetOperationId() != operationID || op.GetResourceId() != doc.CrawlRunId || op.GetResourceType() != "crawl_run" || (op.GetStatus() != "succeeded" && op.GetStatus() != "failed" && op.GetStatus() != "cancelled") {
			return nil, status.Error(codes.FailedPrecondition, "SOURCE_ACTIVE: wait for collection to finish")
		}
	}
	deleter, ok := s.store.(interface {
		DeleteObject(context.Context, string) error
	})
	if !ok {
		return nil, status.Error(codes.Unavailable, "permanent object deletion unavailable")
	}
	fence, e := s.refinement.BlockSourcePurge(ctx, &refinementv1.GetDocumentRequest{DocumentId: doc.DocumentId})
	if e != nil {
		return nil, e
	}
	if fence.GetDocumentId() != doc.DocumentId {
		return nil, status.Error(codes.DataLoss, "source purge fence identity mismatch")
	}
	if _, e = s.pool.Exec(ctx, `UPDATE source_documents SET purge_state='purging' WHERE document_id=$1`, doc.DocumentId); e != nil {
		return nil, idempotency.DBError(e)
	}
	var key string
	if e = s.pool.QueryRow(ctx, `SELECT object_key FROM source_documents WHERE document_id=$1`, doc.DocumentId).Scan(&key); e != nil {
		return nil, readError(e)
	}
	keys := []string{key}
	if doc.BrowserCapture != nil {
		responseKey, e := blob.BrowserResponseKey(doc.CrawlRunId, doc.SourceUrl, doc.BrowserCapture.ResponseSha256)
		if e != nil {
			return nil, status.Error(codes.DataLoss, "invalid response identity")
		}
		keys = append(keys, responseKey)
	}
	for _, key := range keys {
		if e = deleter.DeleteObject(ctx, key); e != nil {
			return nil, status.Error(codes.Unavailable, "source deletion incomplete; retry the same item")
		}
	}
	if _, e = s.pool.Exec(ctx, `UPDATE source_documents SET purge_state='purged',purged_at=COALESCE(purged_at,clock_timestamp()),deleted_at=COALESCE(deleted_at,clock_timestamp()) WHERE document_id=$1`, doc.DocumentId); e != nil {
		return nil, idempotency.DBError(e)
	}
	return s.GetDocument(ctx, r)
}
