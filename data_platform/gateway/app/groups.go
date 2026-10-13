package app

import (
	"bytes"
	collectionv1 "data-platform/gen/collection/v1"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"io"
	"net/http"
	"strings"
)

func groupView(g *collectionv1.SourceGroup) map[string]any {
	return map[string]any{"group_id": g.GetGroupId(), "name": g.GetName(), "created_at": when(g.GetCreatedAt()), "updated_at": when(g.GetUpdatedAt()), "version": g.GetVersion()}
}
func (s *Server) collectionReady(w http.ResponseWriter) bool {
	if s.Collection == nil {
		failure(w, status.Error(codes.Unavailable, "Collection service unavailable"))
		return false
	}
	return true
}
func canonicalGroupID(v string) bool {
	u, e := uuid.Parse(v)
	return e == nil && u != uuid.Nil && u.String() == v
}
func groupBody(w http.ResponseWriter, r *http.Request, rename bool) (string, int64, bool) {
	bad := func() (string, int64, bool) {
		failure(w, status.Error(codes.InvalidArgument, "expected name and, when renaming, positive expected_version; no query, duplicate or unknown fields"))
		return "", 0, false
	}
	if r.URL.RawQuery != "" {
		return bad()
	}
	raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 4096))
	if e != nil {
		return bad()
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	if uniqueJSON(dec) != nil {
		return bad()
	}
	if _, e = dec.Token(); e != io.EOF {
		return bad()
	}
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil || fields == nil {
		return bad()
	}
	name := ""
	var version int64
	for k := range fields {
		if k != "name" && !(rename && k == "expected_version") {
			return bad()
		}
	}
	if string(fields["name"]) == "null" || json.Unmarshal(fields["name"], &name) != nil {
		return bad()
	}
	if rename && (json.Unmarshal(fields["expected_version"], &version) != nil || version < 1) {
		return bad()
	}
	return name, version, true
}
func (s *Server) createGroup(w http.ResponseWriter, r *http.Request) {
	name, _, ok := groupBody(w, r, false)
	if !ok || !s.collectionReady(w) {
		return
	}
	if len(r.Header.Values("Idempotency-Key")) != 1 || r.Header.Get("Idempotency-Key") == "" {
		failure(w, status.Error(codes.InvalidArgument, "one Idempotency-Key is required"))
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	g, e := s.Collection.CreateGroup(ctx, &collectionv1.CreateGroupRequest{Name: name, IdempotencyKey: r.Header.Get("Idempotency-Key")})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, groupView(g))
}
func (s *Server) renameGroup(w http.ResponseWriter, r *http.Request) {
	name, version, ok := groupBody(w, r, true)
	if !ok {
		return
	}
	if !canonicalGroupID(r.PathValue("id")) {
		failure(w, status.Error(codes.InvalidArgument, "canonical nonzero group UUID required"))
		return
	}
	if !s.collectionReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	g, e := s.Collection.RenameGroup(ctx, &collectionv1.RenameGroupRequest{GroupId: r.PathValue("id"), Name: name, ExpectedVersion: version})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, groupView(g))
}
func (s *Server) getGroup(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" || !canonicalGroupID(r.PathValue("id")) {
		failure(w, status.Error(codes.InvalidArgument, "canonical group UUID and no query required"))
		return
	}
	if !s.collectionReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	g, e := s.Collection.GetGroup(ctx, &collectionv1.GetGroupRequest{GroupId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, groupView(g))
}
func (s *Server) listGroups(w http.ResponseWriter, r *http.Request) {
	limit, e := page(r, "q")
	if e != nil {
		failure(w, e)
		return
	}
	if !s.collectionReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	out, e := s.Collection.ListGroups(ctx, &collectionv1.ListGroupsRequest{Limit: limit, Cursor: r.URL.Query().Get("cursor"), Q: r.URL.Query().Get("q")})
	if e != nil {
		failure(w, e)
		return
	}
	items := make([]map[string]any, 0, len(out.Items))
	for _, g := range out.Items {
		items = append(items, groupView(g))
	}
	write(w, 200, map[string]any{"items": items, "next_cursor": out.NextCursor})
}
func (s *Server) groupMembership(w http.ResponseWriter, r *http.Request) {
	group, document := r.PathValue("id"), r.PathValue("document_id")
	body, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 1))
	if !canonicalGroupID(group) || !canonicalGroupID(document) || r.URL.RawQuery != "" || e != nil || len(body) != 0 {
		failure(w, status.Error(codes.InvalidArgument, "one group and document UUID with no query or body required"))
		return
	}
	if !s.collectionReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	present := r.Method == http.MethodPut
	out, e := s.Collection.SetGroupDocument(ctx, &collectionv1.SetGroupDocumentRequest{GroupId: group, DocumentId: document, Present: present})
	if e != nil {
		failure(w, e)
		return
	}
	if out.GetGroupId() != group || out.GetDocumentId() != document || out.GetPresent() != present {
		failure(w, status.Error(codes.DataLoss, "invalid membership receipt"))
		return
	}
	write(w, 200, map[string]any{"group_id": group, "document_id": document, "present": present})
}

func (s *Server) assignDocumentGroup(w http.ResponseWriter, r *http.Request) {
	name, _, ok := groupBody(w, r, false)
	if !ok {
		return
	}
	id := r.PathValue("id")
	if !canonicalGroupID(id) {
		failure(w, status.Error(codes.InvalidArgument, "canonical nonzero document UUID required"))
		return
	}
	if len(r.Header.Values("Idempotency-Key")) != 1 || r.Header.Get("Idempotency-Key") == "" {
		failure(w, status.Error(codes.InvalidArgument, "one Idempotency-Key is required"))
		return
	}
	if !s.collectionReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	out, e := s.Collection.AssignDocumentGroup(ctx, &collectionv1.AssignDocumentGroupRequest{DocumentId: id, Name: name, IdempotencyKey: r.Header.Get("Idempotency-Key")})
	if e != nil {
		failure(w, e)
		return
	}
	if out.GetDocumentId() != id || !canonicalGroupID(out.GetGroup().GetGroupId()) || out.GetGroup().GetVersion() < 1 || out.GetGroup().GetName() != strings.TrimSpace(name) {
		failure(w, status.Error(codes.DataLoss, "invalid assignment receipt"))
		return
	}
	write(w, 200, map[string]any{"document_id": id, "group": groupView(out.Group)})
}
