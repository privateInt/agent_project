package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"net/http/httptest"
	"strings"
	"testing"
)

type groupsClient struct {
	collectionv1.CollectionServiceClient
	calls      int
	assignment *collectionv1.AssignDocumentGroupRequest
	create     *collectionv1.CreateGroupRequest
	rename     *collectionv1.RenameGroupRequest
	membership *collectionv1.SetGroupDocumentRequest
	documents  *collectionv1.ListDocumentsRequest
	wrong      bool
}

func (f *groupsClient) CreateGroup(_ context.Context, r *collectionv1.CreateGroupRequest, _ ...grpc.CallOption) (*collectionv1.SourceGroup, error) {
	f.calls++
	f.create = r
	return &collectionv1.SourceGroup{GroupId: uuid.NewString(), Name: r.Name, Version: 1}, nil
}
func (f *groupsClient) RenameGroup(_ context.Context, r *collectionv1.RenameGroupRequest, _ ...grpc.CallOption) (*collectionv1.SourceGroup, error) {
	f.calls++
	f.rename = r
	return &collectionv1.SourceGroup{GroupId: r.GroupId, Name: r.Name, Version: r.ExpectedVersion + 1}, nil
}
func (f *groupsClient) ListGroups(_ context.Context, _ *collectionv1.ListGroupsRequest, _ ...grpc.CallOption) (*collectionv1.ListGroupsResponse, error) {
	f.calls++
	return &collectionv1.ListGroupsResponse{}, nil
}
func (f *groupsClient) SetGroupDocument(_ context.Context, r *collectionv1.SetGroupDocumentRequest, _ ...grpc.CallOption) (*collectionv1.GroupMembership, error) {
	f.calls++
	f.membership = r
	id := r.DocumentId
	if f.wrong {
		id = uuid.NewString()
	}
	return &collectionv1.GroupMembership{GroupId: r.GroupId, DocumentId: id, Present: r.Present}, nil
}
func (f *groupsClient) ListDocuments(_ context.Context, r *collectionv1.ListDocumentsRequest, _ ...grpc.CallOption) (*collectionv1.ListDocumentsResponse, error) {
	f.calls++
	f.documents = r
	return &collectionv1.ListDocumentsResponse{}, nil
}

func TestGroupRESTContractAndInvalidInputs(t *testing.T) {
	f := &groupsClient{}
	h := New(f, nil).Handler()
	id, doc := uuid.NewString(), uuid.NewString()
	path := "/api/source-groups/" + id
	member := path + "/documents/" + doc
	request := func(method, path, body, key string) int {
		t.Helper()
		r := httptest.NewRequest(method, path, strings.NewReader(body))
		if key != "" {
			r.Header.Set("Idempotency-Key", key)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	for _, c := range []struct{ method, path, body string }{
		{"POST", "/api/source-groups", `{"name":"A","name":"B"}`}, {"POST", "/api/source-groups", `{"name":"A","unknown":1}`}, {"POST", "/api/source-groups", `{"name":null}`},
		{"POST", "/api/source-groups", `{"name":"A"} {}`}, {"POST", "/api/source-groups?all=true", `{"name":"A"}`},
		{"PUT", path, `{"name":"A"}`}, {"PUT", path, `{"name":"A","expected_version":0}`}, {"PUT", path, `{"name":"A","expected_version":1.5}`},
		{"PUT", member, "{}"}, {"DELETE", member, " "}, {"DELETE", member + "?all=true", ""}, {"DELETE", path + "/documents/bad", ""},
	} {
		if code := request(c.method, c.path, c.body, "key"); code != 400 {
			t.Fatal(c, code)
		}
	}
	if request("POST", "/api/source-groups", `{"name":"A"}`, "") != 400 || f.calls != 0 {
		t.Fatal("bad inputs forwarded", f.calls)
	}
	if request("POST", "/api/source-groups", `{"name":"한글 그룹"}`, "key") != 200 || f.create.Name != "한글 그룹" || f.create.IdempotencyKey != "key" {
		t.Fatal(f.create)
	}
	if request("PUT", path, `{"name":"B","expected_version":2}`, "") != 200 || f.rename.ExpectedVersion != 2 || f.rename.GroupId != id {
		t.Fatal(f.rename)
	}
	for _, method := range []string{"PUT", "DELETE"} {
		if request(method, member, "", "") != 200 || f.membership.GroupId != id || f.membership.DocumentId != doc || f.membership.Present != (method == "PUT") {
			t.Fatal(method, f.membership)
		}
	}
	f.wrong = true
	if request("DELETE", member, "", "") != 502 {
		t.Fatal("wrong receipt accepted")
	}
	if request("GET", "/api/source-documents?group_id="+id+"&limit=2", "", "") != 200 || f.documents.GroupId != id || f.documents.Limit != 2 {
		t.Fatal(f.documents)
	}
	if request("GET", "/api/source-groups", "", "") != 200 {
		t.Fatal("list")
	}
	if request("DELETE", path, "", "") != 405 {
		t.Fatal("must not delete a whole group or its files")
	}
}

func (f *groupsClient) AssignDocumentGroup(_ context.Context, r *collectionv1.AssignDocumentGroupRequest, _ ...grpc.CallOption) (*collectionv1.DocumentGroupAssignment, error) {
	f.calls++
	f.assignment = r
	id := r.DocumentId
	if f.wrong {
		id = uuid.NewString()
	}
	return &collectionv1.DocumentGroupAssignment{DocumentId: id, Group: &collectionv1.SourceGroup{GroupId: uuid.NewString(), Name: strings.TrimSpace(r.Name), Version: 1}}, nil
}
func TestAssignGroupByNameRouteAndSourceColumn(t *testing.T) {
	f := &groupsClient{}
	h := New(f, nil).Handler()
	id := uuid.NewString()
	path := "/api/source-documents/" + id + "/group"
	request := func(path, body, key string) int {
		r := httptest.NewRequest("PUT", path, strings.NewReader(body))
		if key != "" {
			r.Header.Set("Idempotency-Key", key)
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		return w.Code
	}
	for _, bad := range []struct{ path, body, key string }{{path, `{"name":"A"}`, ""}, {path, `{"name":"A","name":"B"}`, "key"}, {path + "?all=true", `{"name":"A"}`, "key"}, {"/api/source-documents/bad/group", `{"name":"A"}`, "key"}} {
		if request(bad.path, bad.body, bad.key) != 400 {
			t.Fatal(bad)
		}
	}
	if f.calls != 0 {
		t.Fatal("invalid request forwarded")
	}
	if request(path, `{"name":" A "}`, "key") != 200 || f.assignment.DocumentId != id || f.assignment.IdempotencyKey != "key" {
		t.Fatal(f.assignment)
	}
	f.wrong = true
	if request(path, `{"name":"A"}`, "key") != 502 {
		t.Fatal("wrong receipt accepted")
	}
	group := &collectionv1.SourceGroup{GroupId: uuid.NewString(), Name: "현재 그룹", Version: 2}
	raw, e := json.Marshal(documentView(&collectionv1.SourceDocument{DocumentId: id, Groups: []*collectionv1.SourceGroup{group}}))
	if e != nil {
		t.Fatal(e)
	}
	var result struct {
		Groups []struct {
			GroupID string `json:"group_id"`
			Name    string `json:"name"`
			Version int64  `json:"version"`
		} `json:"groups"`
	}
	if json.Unmarshal(raw, &result) != nil || len(result.Groups) != 1 || result.Groups[0].GroupID != group.GroupId || result.Groups[0].Name != group.Name || result.Groups[0].Version != 2 {
		t.Fatal(string(raw))
	}
}
