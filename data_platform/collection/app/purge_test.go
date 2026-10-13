package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"errors"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"testing"
)

type purgeUploadStore struct {
	*uploadStore
	failDelete bool
	deletes    int
}

func (m *purgeUploadStore) DeleteObject(_ context.Context, key string) error {
	m.deletes++
	if m.failDelete {
		return errors.New("object lock")
	}
	delete(m.objects, key)
	return nil
}

type purgeReference struct {
	refinementv1.RefinementServiceClient
	blocked bool
	wrong   bool
}

func (f *purgeReference) BlockSourcePurge(_ context.Context, r *refinementv1.GetDocumentRequest, _ ...grpc.CallOption) (*refinementv1.PurgeFence, error) {
	if f.blocked {
		return nil, status.Error(codes.FailedPrecondition, "SOURCE_HAS_RESULTS")
	}
	id := r.DocumentId
	if f.wrong {
		id = uuid.NewString()
	}
	return &refinementv1.PurgeFence{DocumentId: id}, nil
}
func TestPurgeSourceReferencesFailureRetryAndUploadReplay(t *testing.T) {
	s, m := uploadFixture(t)
	ctx := context.Background()
	store := &purgeUploadStore{uploadStore: m}
	s.store = store
	data := []byte("original")
	meta := uploadMetadata(uuid.NewString(), data)
	doc, e := s.registerUpload(ctx, meta, data)
	if e != nil {
		t.Fatal(e)
	}
	target := &collectionv1.GetDocumentRequest{DocumentId: doc.DocumentId}
	refs := &purgeReference{blocked: true}
	s.SetRefinement(refs)
	if _, e = s.PurgeDocument(ctx, target); status.Code(e) != codes.FailedPrecondition || store.deletes != 0 {
		t.Fatal("referenced file deleted", e)
	}
	refs.blocked = false
	refs.wrong = true
	if _, e = s.PurgeDocument(ctx, target); status.Code(e) != codes.DataLoss || store.deletes != 0 {
		t.Fatal("wrong fence accepted", e)
	}
	refs.wrong = false
	store.failDelete = true
	if _, e = s.PurgeDocument(ctx, target); status.Code(e) != codes.Unavailable {
		t.Fatal(e)
	}
	pending, e := s.GetDocument(ctx, target)
	if e != nil || pending.PurgeState != "purging" || pending.PurgedAt != nil || pending.State == "ready" {
		t.Fatal(pending, e)
	}
	store.failDelete = false
	result, e := s.PurgeDocument(ctx, target)
	if e != nil || result.PurgedAt == nil || len(store.objects) != 0 {
		t.Fatal(result, e)
	}
	s.refinement = nil
	replay, e := s.PurgeDocument(ctx, target)
	if e != nil || !replay.PurgedAt.AsTime().Equal(result.PurgedAt.AsTime()) {
		t.Fatal("retry was not stable", e)
	}
	if _, e = s.registerUpload(ctx, meta, data); e != nil || len(store.objects) != 0 || m.puts != 1 {
		t.Fatal("upload replay restored deleted bytes", e)
	}
	page, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{IncludeDeleted: true})
	if e != nil || len(page.Items) != 0 {
		t.Fatal("permanently deleted source listed", e)
	}
}
