package app

import (
	"bytes"
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"strings"
	"sync"
	"testing"
)

func makeGroup(t *testing.T, s *Service, name string) *collectionv1.SourceGroup {
	t.Helper()
	g, e := s.CreateGroup(context.Background(), &collectionv1.CreateGroupRequest{Name: name, IdempotencyKey: uuid.NewString()})
	if e != nil {
		t.Fatal(e)
	}
	return g
}
func TestGroupsCreateReplayRenameAndValidation(t *testing.T) {
	s, _ := uploadFixture(t)
	ctx := context.Background()
	for _, name := range []string{"", "  ", "a\x00b", strings.Repeat("가", 201)} {
		if _, e := s.CreateGroup(ctx, &collectionv1.CreateGroupRequest{Name: name, IdempotencyKey: uuid.NewString()}); status.Code(e) != codes.InvalidArgument {
			t.Fatal(name, e)
		}
	}
	if _, e := s.CreateGroup(ctx, &collectionv1.CreateGroupRequest{Name: "A"}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
	r := &collectionv1.CreateGroupRequest{Name: "  프로젝트 A  ", IdempotencyKey: uuid.NewString()}
	var wg sync.WaitGroup
	out := make(chan *collectionv1.SourceGroup, 6)
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			g, e := s.CreateGroup(ctx, r)
			if e != nil {
				t.Error(e)
			}
			out <- g
		}()
	}
	wg.Wait()
	close(out)
	var first *collectionv1.SourceGroup
	for g := range out {
		if first == nil {
			first = g
		}
		if g == nil || !proto.Equal(first, g) {
			t.Fatal("create replay differs", first, g)
		}
	}
	if first.Name != "프로젝트 A" || first.Version != 1 {
		t.Fatal(first)
	}
	if _, e := s.CreateGroup(ctx, &collectionv1.CreateGroupRequest{Name: "프로젝트 A", IdempotencyKey: uuid.NewString()}); status.Code(e) != codes.AlreadyExists {
		t.Fatal("duplicate name", e)
	}
	if _, e := s.CreateGroup(ctx, &collectionv1.CreateGroupRequest{Name: "other", IdempotencyKey: r.IdempotencyKey}); status.Code(e) != codes.AlreadyExists {
		t.Fatal("changed key", e)
	}
	rename := &collectionv1.RenameGroupRequest{GroupId: first.GroupId, Name: "프로젝트 B", ExpectedVersion: 1}
	g, e := s.RenameGroup(ctx, rename)
	if e != nil || g.GetVersion() != 2 {
		t.Fatal(g, e)
	}
	replay, e := s.RenameGroup(ctx, rename)
	if e != nil || !proto.Equal(g, replay) {
		t.Fatal("lost rename reply", replay, e)
	}
	if _, e = s.RenameGroup(ctx, &collectionv1.RenameGroupRequest{GroupId: first.GroupId, Name: "undo", ExpectedVersion: 1}); status.Code(e) != codes.AlreadyExists {
		t.Fatal("stale rename", e)
	}
	recreated := makeGroup(t, s, "프로젝트 A")
	if recreated.GroupId == first.GroupId {
		t.Fatal("recreated name changed identity")
	}
	if _, e = s.RenameGroup(ctx, &collectionv1.RenameGroupRequest{GroupId: first.GroupId, Name: recreated.Name, ExpectedVersion: 2}); status.Code(e) != codes.AlreadyExists {
		t.Fatal("name collision", e)
	}
	replay, e = s.CreateGroup(ctx, r)
	if e != nil || !proto.Equal(replay, first) {
		t.Fatal("creation receipt mutated after rename", e)
	}
	current, e := s.GetGroup(ctx, &collectionv1.GetGroupRequest{GroupId: first.GroupId})
	if e != nil || current.Name != "프로젝트 B" {
		t.Fatal(current, e)
	}
	page, e := s.ListGroups(ctx, &collectionv1.ListGroupsRequest{Limit: 1})
	if e != nil || len(page.Items) != 1 || page.NextCursor == "" {
		t.Fatal(page, e)
	}
	next, e := s.ListGroups(ctx, &collectionv1.ListGroupsRequest{Limit: 1, Cursor: page.NextCursor})
	if e != nil || len(next.Items) != 1 || next.Items[0].GroupId == page.Items[0].GroupId {
		t.Fatal(next, e)
	}
	if _, e = s.ListGroups(ctx, &collectionv1.ListGroupsRequest{Limit: 1, Q: "changed", Cursor: page.NextCursor}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
	if e = InitSchema(ctx, s.pool); e != nil {
		t.Fatal(e)
	}
	noUploadJobs(t, s)
}

func TestGroupsMembershipAndPaginationPreserveOriginals(t *testing.T) {
	s, store := uploadFixture(t)
	ctx := context.Background()
	a := makeGroup(t, s, "A")
	b := makeGroup(t, s, "B")
	empty := makeGroup(t, s, "empty")
	data := []byte("immutable source\n")
	var docs []*collectionv1.SourceDocument
	for i := 0; i < 3; i++ {
		d, e := s.registerUpload(ctx, uploadMetadata(uuid.NewString(), data), data)
		if e != nil {
			t.Fatal(e)
		}
		docs = append(docs, d)
	}
	set := func(g, id string, present bool) {
		t.Helper()
		r, e := s.SetGroupDocument(ctx, &collectionv1.SetGroupDocumentRequest{GroupId: g, DocumentId: id, Present: present})
		if e != nil || r.GetPresent() != present {
			t.Fatal(r, e)
		}
	}
	for _, d := range docs {
		set(a.GroupId, d.DocumentId, true)
		set(a.GroupId, d.DocumentId, true)
	}
	set(b.GroupId, docs[0].DocumentId, true)
	first, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: a.GroupId, Limit: 2})
	if e != nil || len(first.Items) != 2 || first.NextCursor == "" {
		t.Fatal(first, e)
	}
	next, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: a.GroupId, Limit: 2, Cursor: first.NextCursor})
	if e != nil || len(next.Items) != 1 || next.Items[0].DocumentId == first.Items[0].DocumentId || next.Items[0].DocumentId == first.Items[1].DocumentId {
		t.Fatal(next, e)
	}
	for _, g := range []string{b.GroupId, ""} {
		if _, e = s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: g, Cursor: first.NextCursor}); status.Code(e) != codes.InvalidArgument {
			t.Fatal("cursor crossed group", e)
		}
	}
	set(a.GroupId, docs[0].DocumentId, false)
	set(a.GroupId, docs[0].DocumentId, false)
	for id, want := range map[string]int{a.GroupId: 2, b.GroupId: 1, empty.GroupId: 0, "": 3} {
		p, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: id})
		if e != nil || len(p.Items) != want {
			t.Fatal(id, p, e)
		}
	}
	for _, before := range docs {
		after, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: before.DocumentId})
		if after != nil {
			after.Groups = nil
		} // Group catalog metadata is mutable; source content is not.
		if e != nil || !proto.Equal(before, after) {
			t.Fatal("source mutated by group", after, e)
		}
	}
	if store.puts != 3 || len(store.objects) != 3 {
		t.Fatal("membership copied or deleted storage")
	}
	for _, body := range store.objects {
		if !bytes.Equal(body, data) {
			t.Fatal("bytes mutated")
		}
	}
	noUploadJobs(t, s)
}

func TestGroupsDeletedPurgedAndUnknownMembership(t *testing.T) {
	s, _ := uploadFixture(t)
	ctx := context.Background()
	g := makeGroup(t, s, "available")
	d, e := s.registerUpload(ctx, uploadMetadata(uuid.NewString(), []byte("original")), []byte("original"))
	if e != nil {
		t.Fatal(e)
	}
	r := &collectionv1.SetGroupDocumentRequest{GroupId: g.GroupId, DocumentId: d.DocumentId, Present: true}
	if _, e = s.SetGroupDocument(ctx, r); e != nil {
		t.Fatal(e)
	}
	if _, e = s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: d.DocumentId}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.SetGroupDocument(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal(e)
	}
	p, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: g.GroupId})
	if e != nil || len(p.Items) != 0 {
		t.Fatal(p, e)
	}
	p, e = s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: g.GroupId, IncludeDeleted: true})
	if e != nil || len(p.Items) != 1 {
		t.Fatal(p, e)
	}
	if _, e = s.pool.Exec(ctx, `UPDATE source_documents SET purge_state='purged',purged_at=NOW() WHERE document_id=$1`, d.DocumentId); e != nil {
		t.Fatal(e)
	}
	p, e = s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: g.GroupId, IncludeDeleted: true})
	if e != nil || len(p.Items) != 0 {
		t.Fatal(p, e)
	}
	r.Present = false
	if _, e = s.SetGroupDocument(ctx, r); e != nil {
		t.Fatal("cannot remove purged membership", e)
	}
	r.Present = true
	if _, e = s.SetGroupDocument(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal(e)
	}
	for _, bad := range []*collectionv1.SetGroupDocumentRequest{{GroupId: uuid.NewString(), DocumentId: d.DocumentId}, {GroupId: g.GroupId, DocumentId: uuid.NewString()}} {
		if _, e = s.SetGroupDocument(ctx, bad); status.Code(e) != codes.NotFound {
			t.Fatal(e)
		}
	}
	if _, e = s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{GroupId: uuid.NewString()}); status.Code(e) != codes.NotFound {
		t.Fatal(e)
	}
	if _, e = s.SetGroupDocument(ctx, &collectionv1.SetGroupDocumentRequest{GroupId: "all", DocumentId: d.DocumentId}); status.Code(e) != codes.InvalidArgument {
		t.Fatal(e)
	}
}

func TestAssignGroupNameAtomicConcurrentReuseAndReplay(t *testing.T) {
	s, store := uploadFixture(t)
	ctx := context.Background()
	var docs []*collectionv1.SourceDocument
	for i := 0; i < 3; i++ {
		d, e := s.registerUpload(ctx, uploadMetadata(uuid.NewString(), []byte("original")), []byte("original"))
		if e != nil {
			t.Fatal(e)
		}
		docs = append(docs, d)
	}
	var wg sync.WaitGroup
	results := make(chan *collectionv1.DocumentGroupAssignment, 6)
	for i := 0; i < 6; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			out, e := s.AssignDocumentGroup(ctx, &collectionv1.AssignDocumentGroupRequest{DocumentId: docs[i%3].DocumentId, Name: "  공유 그룹  ", IdempotencyKey: uuid.NewString()})
			if e != nil {
				t.Error(e)
			}
			results <- out
		}(i)
	}
	wg.Wait()
	close(results)
	var group *collectionv1.SourceGroup
	for out := range results {
		if group == nil {
			group = out.GetGroup()
		}
		if out == nil || !proto.Equal(out.Group, group) {
			t.Fatal("concurrent same-name assignment differs", out)
		}
	}
	var count int
	if e := s.pool.QueryRow(ctx, `SELECT count(*) FROM source_groups`).Scan(&count); e != nil || count != 1 {
		t.Fatal(count, e)
	}
	if e := s.pool.QueryRow(ctx, `SELECT count(*) FROM source_group_members`).Scan(&count); e != nil || count != 3 {
		t.Fatal(count, e)
	}
	page, e := s.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{Limit: 2, GroupId: group.GroupId})
	if e != nil || len(page.Items) != 2 || page.NextCursor == "" {
		t.Fatal(page, e)
	}
	for _, d := range page.Items {
		if len(d.Groups) != 1 || !proto.Equal(d.Groups[0], group) {
			t.Fatal("missing row group", d)
		}
	}
	r := &collectionv1.AssignDocumentGroupRequest{DocumentId: docs[0].DocumentId, Name: "공유 그룹", IdempotencyKey: uuid.NewString()}
	first, e := s.AssignDocumentGroup(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	replay, e := s.AssignDocumentGroup(ctx, r)
	if e != nil || !proto.Equal(first, replay) {
		t.Fatal("lost assignment reply", e)
	}
	for _, changed := range []*collectionv1.AssignDocumentGroupRequest{{DocumentId: docs[1].DocumentId, Name: r.Name, IdempotencyKey: r.IdempotencyKey}, {DocumentId: r.DocumentId, Name: "다른 그룹", IdempotencyKey: r.IdempotencyKey}} {
		if _, e = s.AssignDocumentGroup(ctx, changed); status.Code(e) != codes.AlreadyExists {
			t.Fatal("changed intent accepted", e)
		}
	}
	other := makeGroup(t, s, "다른 그룹")
	if _, e = s.AssignDocumentGroup(ctx, &collectionv1.AssignDocumentGroupRequest{DocumentId: r.DocumentId, Name: other.Name, IdempotencyKey: uuid.NewString()}); e != nil {
		t.Fatal(e)
	}
	if _, e = s.SetGroupDocument(ctx, &collectionv1.SetGroupDocumentRequest{GroupId: group.GroupId, DocumentId: r.DocumentId}); e != nil {
		t.Fatal(e)
	}
	// Replaying an old receipt must not resurrect a later explicitly removed link.
	if _, e = s.AssignDocumentGroup(ctx, r); e != nil {
		t.Fatal(e)
	}
	d, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: r.DocumentId})
	if e != nil || len(d.Groups) != 1 || d.Groups[0].GroupId != other.GroupId {
		t.Fatal(d, e)
	}
	if _, e = s.RenameGroup(ctx, &collectionv1.RenameGroupRequest{GroupId: other.GroupId, Name: "새 이름", ExpectedVersion: 1}); e != nil {
		t.Fatal(e)
	}
	d, e = s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: r.DocumentId})
	if e != nil || len(d.Groups) != 1 || d.Groups[0].Name != "새 이름" {
		t.Fatal(d, e)
	}
	d.Groups = nil
	if !proto.Equal(d, docs[0]) || store.puts != 3 || len(store.objects) != 3 {
		t.Fatal("assignment altered original", d)
	}
	noUploadJobs(t, s)
}

func TestAssignGroupFailureRollsBackNewGroupAndMembership(t *testing.T) {
	s, _ := uploadFixture(t)
	ctx := context.Background()
	d, e := s.registerUpload(ctx, uploadMetadata(uuid.NewString(), []byte("original")), []byte("original"))
	if e != nil {
		t.Fatal(e)
	}
	r := &collectionv1.AssignDocumentGroupRequest{DocumentId: d.DocumentId, Name: "new", IdempotencyKey: uuid.NewString()}
	// Force failure between group resolution and membership: neither may commit.
	if _, e = s.pool.Exec(ctx, `ALTER TABLE source_group_members ADD CONSTRAINT test_group_failure CHECK(false)`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.AssignDocumentGroup(ctx, r); e == nil {
		t.Fatal("membership failure ignored")
	}
	var n int
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM source_groups`).Scan(&n); e != nil || n != 0 {
		t.Fatal("orphan group", n, e)
	}
	if _, e = s.pool.Exec(ctx, `ALTER TABLE source_group_members DROP CONSTRAINT test_group_failure`); e != nil {
		t.Fatal(e)
	}
	if _, e = s.AssignDocumentGroup(ctx, r); e != nil {
		t.Fatal("same key cannot retry after rollback", e)
	}
	if _, e = s.DeleteDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: d.DocumentId}); e != nil {
		t.Fatal(e)
	}
	for _, bad := range []*collectionv1.AssignDocumentGroupRequest{{DocumentId: d.DocumentId, Name: "deleted", IdempotencyKey: uuid.NewString()}, {DocumentId: uuid.NewString(), Name: "unknown", IdempotencyKey: uuid.NewString()}} {
		if _, e = s.AssignDocumentGroup(ctx, bad); e == nil {
			t.Fatal("invalid source accepted")
		}
	}
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM source_groups`).Scan(&n); e != nil || n != 1 {
		t.Fatal("failed requests left groups", n, e)
	}
}
