package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/idempotency"
	"data-platform/internal/listing"
)

func (s *Service) ListCrawls(ctx context.Context, r *collectionv1.ListCrawlsRequest) (*collectionv1.ListCrawlsResponse, error) {
	q, e := listing.Text(r.GetQ())
	if e != nil {
		return nil, e
	}
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "crawl-runs", q)
	if e != nil {
		return nil, e
	}
	query := listing.Query{}
	query.Contains(q, "name", "source_url")
	suffix := query.Finish(page, "created_at", "crawl_run_id")
	rows, e := s.pool.Query(ctx, runSelect+suffix, query.Args...)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer rows.Close()
	out := &collectionv1.ListCrawlsResponse{}
	for rows.Next() {
		if len(out.Items) == page.Limit {
			last := out.Items[len(out.Items)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.CrawlRunId)
			break
		}
		run, e := scanRun(rows)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
		out.Items = append(out.Items, run)
	}
	return out, idempotency.DBError(rows.Err())
}
func (s *Service) ListDocuments(ctx context.Context, r *collectionv1.ListDocumentsRequest) (*collectionv1.ListDocumentsResponse, error) {
	q, e := listing.Text(r.GetQ())
	if e != nil {
		return nil, e
	}
	filters := []string{q}
	if r.GetGroupId() != "" {
		if _, e := s.GetGroup(ctx, &collectionv1.GetGroupRequest{GroupId: r.GroupId}); e != nil {
			return nil, e
		}
		filters = append(filters, "group:"+r.GroupId)
	}
	if r.GetIncludeDeleted() {
		filters = append(filters, "include-deleted")
	}
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "source-documents", filters...)
	if e != nil {
		return nil, e
	}
	query := listing.Query{}
	if r.GetGroupId() != "" {
		query.Where("EXISTS (SELECT 1 FROM source_group_members gm WHERE gm.document_id=source_documents.document_id AND gm.group_id=" + query.Param(r.GroupId) + ")")
	}
	if !r.GetIncludeDeleted() {
		query.IsNull("deleted_at")
	}
	query.IsNull("purged_at")
	query.Contains(q, "name", "source_url", "original_filename")
	suffix := query.Finish(page, "created_at", "document_id")
	rows, e := s.pool.Query(ctx, docSelect+suffix, query.Args...)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer rows.Close()
	out := &collectionv1.ListDocumentsResponse{}
	for rows.Next() {
		if len(out.Items) == page.Limit {
			last := out.Items[len(out.Items)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.DocumentId)
			break
		}
		doc, e := s.scanDoc(rows)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
		out.Items = append(out.Items, doc)
	}
	if e := rows.Err(); e != nil {
		return nil, idempotency.DBError(e)
	}
	rows.Close() // Release the list connection before the bounded batch membership query.
	if e := s.attachDocumentGroups(ctx, out.Items); e != nil {
		return nil, e
	}
	return out, nil
}
