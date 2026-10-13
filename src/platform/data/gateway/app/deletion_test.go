package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"net/http/httptest"
	"strings"
	"testing"
	"time"
)

type deletionCollection struct {
	collectionv1.CollectionServiceClient
	target string
	calls  int
	err    error
	wrong  bool
}

func (f *deletionCollection) DeleteDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	f.calls++
	f.target = r.DocumentId
	if f.err != nil {
		return nil, f.err
	}
	id := r.DocumentId
	if f.wrong {
		id = uuid.NewString()
	}
	return &collectionv1.SourceDocument{DocumentId: id, DeletedAt: timestamppb.New(time.Unix(100, 0))}, nil
}

type deletionRefinement struct {
	refinementv1.RefinementServiceClient
	target  string
	calls   int
	err     error
	missing bool
}

func (f *deletionRefinement) DeleteRun(_ context.Context, r *refinementv1.GetRunRequest, _ ...grpc.CallOption) (*refinementv1.RefinementRun, error) {
	f.calls++
	f.target = r.RefinementRunId
	if f.err != nil {
		return nil, f.err
	}
	out := &refinementv1.RefinementRun{RefinementRunId: r.RefinementRunId}
	if !f.missing {
		out.DeletedAt = timestamppb.New(time.Unix(100, 0))
	}
	return out, nil
}
func TestDeletionRoutesValidateScopeAndReturnedIdentity(t *testing.T) {
	col := &deletionCollection{}
	ref := &deletionRefinement{}
	s := New(col, nil)
	s.Refinement = ref
	handler := s.Handler()
	id := uuid.NewString()
	for _, collection := range []string{"source-documents", "refinement-runs"} {
		path := "/api/" + collection + "/" + id
		for _, invalid := range []struct{ path, body string }{{path + "?all=true", ""}, {path, "{}"}, {path, " "}, {"/api/" + collection + "/bad", ""}} {
			w := httptest.NewRecorder()
			handler.ServeHTTP(w, httptest.NewRequest("DELETE", invalid.path, strings.NewReader(invalid.body)))
			if w.Code != 400 {
				t.Fatal(w.Code, w.Body.String())
			}
		}
	}
	if col.calls != 0 || ref.calls != 0 {
		t.Fatal("invalid DELETE forwarded")
	}
	for _, collection := range []string{"source-documents", "refinement-runs"} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest("DELETE", "/api/"+collection+"/"+id, nil))
		var body map[string]any
		if w.Code != 200 || json.Unmarshal(w.Body.Bytes(), &body) != nil || body["deleted_at"] == nil {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	if col.target != id || ref.target != id || col.calls != 1 || ref.calls != 1 {
		t.Fatal("wrong target")
	}
	col.wrong = true
	ref.missing = true
	for _, collection := range []string{"source-documents", "refinement-runs"} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest("DELETE", "/api/"+collection+"/"+id, nil))
		if w.Code != 502 {
			t.Fatal("unconfirmed removal reported success", w.Code)
		}
	}
}
func TestDeletionErrorsAndUnsupportedTargetsRemainVisible(t *testing.T) {
	col := &deletionCollection{}
	ref := &deletionRefinement{}
	s := New(col, nil)
	s.Refinement = ref
	id := uuid.NewString()
	for _, c := range []struct {
		code codes.Code
		http int
	}{{codes.NotFound, 404}, {codes.FailedPrecondition, 409}, {codes.Unavailable, 503}} {
		col.err = status.Error(c.code, "not removable")
		ref.err = col.err
		for _, collection := range []string{"source-documents", "refinement-runs"} {
			w := httptest.NewRecorder()
			s.Handler().ServeHTTP(w, httptest.NewRequest("DELETE", "/api/"+collection+"/"+id, nil))
			if w.Code != c.http {
				t.Fatal(w.Code, c)
			}
		}
	}
	for _, collection := range []string{"canonical-documents", "crawl-runs", "operations"} {
		w := httptest.NewRecorder()
		s.Handler().ServeHTTP(w, httptest.NewRequest("DELETE", "/api/"+collection+"/"+id, nil))
		if w.Code != 405 {
			t.Fatal("unexpected deletion target", collection, w.Code)
		}
	}
	w := httptest.NewRecorder()
	New(nil, nil).Handler().ServeHTTP(w, httptest.NewRequest("DELETE", "/api/source-documents/"+id, nil))
	if w.Code != 503 {
		t.Fatal(w.Code)
	}
}
