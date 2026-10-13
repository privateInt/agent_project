package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"sync"
	"testing"
)

func TestDeleteSourcePreservesOriginalAndReplayButExcludesAllListPages(t *testing.T) {
	s, ops, store := fixture(t)
	ctx := context.Background()
	var documents []*collectionv1.SourceDocument
	var completion *collectionv1.CompleteCrawlRequest
	for i := 0; i < 3; i++ {
		r := prepareResult(t, s, ops, store)
		run, e := s.CompleteCrawl(ctx, r)
		if e != nil {
			t.Fatal(e)
		}
		doc, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: run.OutputDocumentId})
		if e != nil {
			t.Fatal(e)
		}
		documents = append(documents, doc)
		if i == 1 {
			completion = r
		}
	}
	target := documents[1]
	beforeOpens := store.opens
	var wg sync.WaitGroup
	results := make(chan *collectionv1.SourceDocument, 6)
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			d, e := s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: target.DocumentId})
			if e != nil {
				t.Error(e)
			}
			results <- d
		}()
	}
	wg.Wait()
	close(results)
	var first *collectionv1.SourceDocument
	for d := range results {
		if d.GetDeletedAt() == nil {
			t.Fatal("missing deletion record")
		}
		if first == nil {
			first = d
		}
		if !proto.Equal(first.DeletedAt, d.DeletedAt) {
			t.Fatal("retry changed timestamp")
		}
	}
	if store.opens != beforeOpens || store.body != "original" {
		t.Fatal("delete touched immutable storage")
	}
	if e := InitSchema(ctx, s.pool); e != nil {
		t.Fatal(e)
	}
	got, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: target.DocumentId})
	if e != nil || got.GetDeletedAt() == nil || got.GetSha256() != target.Sha256 || got.GetArtifactUri() != target.ArtifactUri {
		t.Fatal("lost provenance", e)
	}
	listed, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{Limit: 1, Q: "source"})
	if e != nil || len(listed.Items) != 1 || listed.NextCursor == "" || listed.Items[0].DocumentId == target.DocumentId {
		t.Fatal("first page", listed, e)
	}
	next, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{Limit: 1, Q: "source", Cursor: listed.NextCursor})
	if e != nil || len(next.Items) != 1 || next.NextCursor != "" || next.Items[0].DocumentId == target.DocumentId {
		t.Fatal("second page", next, e)
	}
	replay, e := s.CompleteCrawl(ctx, completion)
	if e != nil || replay.GetOutputDocumentId() != target.DocumentId {
		t.Fatal("completion replay changed source", e)
	}
	if _, e = s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: uuid.NewString()}); status.Code(e) != codes.NotFound {
		t.Fatal(e)
	}
	if _, e = s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: target.DocumentId, HttpResponse: true}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
	if _, e = s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: "bad"}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
}
