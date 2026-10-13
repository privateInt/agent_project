package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
	"net/http/httptest"
	"strings"
	"testing"
)

type purgeCollection struct {
	collectionv1.CollectionServiceClient
	calls int
	bad   bool
}

func (f *purgeCollection) PurgeDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	f.calls++
	v := &collectionv1.SourceDocument{DocumentId: r.DocumentId, PurgeState: "purged", PurgedAt: timestamppb.Now()}
	if f.bad {
		v.PurgedAt = nil
	}
	return v, nil
}

type purgeRefinement struct {
	refinementv1.RefinementServiceClient
	calls int
	bad   bool
}

func (f *purgeRefinement) PurgeRun(_ context.Context, r *refinementv1.GetRunRequest, _ ...grpc.CallOption) (*refinementv1.RefinementRun, error) {
	f.calls++
	id := r.RefinementRunId
	if f.bad {
		id = uuid.NewString()
	}
	return &refinementv1.RefinementRun{RefinementRunId: id, PurgeState: "purged", PurgedAt: timestamppb.Now()}, nil
}
func TestPurgeRoutesRequireExplicitPathAndExactReceipt(t *testing.T) {
	c := &purgeCollection{}
	f := &purgeRefinement{}
	s := New(c, nil)
	s.Refinement = f
	h := s.Handler()
	id := uuid.NewString()
	for _, collection := range []string{"source-documents", "refinement-runs"} {
		path := "/api/" + collection + "/" + id + "/purge"
		for _, v := range []struct{ suffix, body string }{{"?all=true", ""}, {"", "{}"}} {
			w := httptest.NewRecorder()
			h.ServeHTTP(w, httptest.NewRequest("POST", path+v.suffix, strings.NewReader(v.body)))
			if w.Code != 400 {
				t.Fatal(w.Code)
			}
		}
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("POST", path, nil))
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	if c.calls != 1 || f.calls != 1 {
		t.Fatal("invalid request forwarded", c.calls, f.calls)
	}
	c.bad = true
	f.bad = true
	for _, collection := range []string{"source-documents", "refinement-runs"} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("POST", "/api/"+collection+"/"+id+"/purge", nil))
		if w.Code == 200 {
			t.Fatal("invalid receipt accepted")
		}
	}
}
