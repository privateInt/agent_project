package app

import (
	"context"
	refinementv1 "data-platform/gen/refinement/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeRefinement struct {
	refinementv1.RefinementServiceClient
	creates int
	key     string
}

func (f *fakeRefinement) CreateRun(_ context.Context, r *refinementv1.CreateRunRequest, _ ...grpc.CallOption) (*refinementv1.RunAccepted, error) {
	f.creates++
	f.key = r.IdempotencyKey
	return &refinementv1.RunAccepted{RefinementRunId: "run", OperationId: "op"}, nil
}
func TestRefinementRejectsAmbiguousJSON(t *testing.T) {
	for _, body := range []string{`null`, `{"name":"x","document_id":"a","document_id":"b"}`, `{"name":"x","document_id":"a","rules_version":null}`, `{"name":"x","document_id":"a","gpu_count":1}`, `{"Name":"x","document_id":"a"}`, `{"name":"x","document_id":null}`, `{"name":"x","document_id":"a"} {}`} {
		if _, e := decodeRefinement([]byte(body)); e == nil {
			t.Fatal("ambiguous body accepted", body)
		}
	}
}
func TestRefinementPostRequiresOneKeyAndForwardsIt(t *testing.T) {
	f := &fakeRefinement{}
	server := New(nil, nil)
	server.Refinement = f
	handler := server.Handler()
	body := `{"name":"x","document_id":"a"}`
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest("POST", "/api/refinement-runs", strings.NewReader(body)))
	if w.Code != 400 || f.creates != 0 {
		t.Fatal(w.Code)
	}
	r := httptest.NewRequest("POST", "/api/refinement-runs", strings.NewReader(body))
	r.Header.Set("Idempotency-Key", "ref-one")
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 202 || f.creates != 1 || f.key != "ref-one" {
		t.Fatal(w.Code)
	}
	r = httptest.NewRequest("POST", "/api/refinement-runs", strings.NewReader(body))
	r.Header.Add("Idempotency-Key", "a")
	r.Header.Add("Idempotency-Key", "b")
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 400 || f.creates != 1 {
		t.Fatal("multiple keys accepted")
	}
}

type ocrRouteRefinement struct {
	*fakeRefinement
	ocr        int
	deployment string
}

func (f *ocrRouteRefinement) CreateOCRRun(_ context.Context, r *refinementv1.CreateRunRequest, _ ...grpc.CallOption) (*refinementv1.RunAccepted, error) {
	f.ocr++
	f.deployment = r.OcrDeploymentId
	return nil, status.Error(codes.Unimplemented, "old server")
}
func TestOCRRefinementCannotDowngradeToCPU(t *testing.T) {
	f := &ocrRouteRefinement{fakeRefinement: &fakeRefinement{}}
	server := New(nil, nil)
	server.Refinement = f
	req := httptest.NewRequest("POST", "/api/refinement-runs", strings.NewReader(`{"name":"ocr","document_id":"source","ocr_deployment_id":"deployment"}`))
	req.Header.Set("Idempotency-Key", "ocr-key")
	w := httptest.NewRecorder()
	server.Handler().ServeHTTP(w, req)
	if w.Code == 202 || f.creates != 0 || f.ocr != 1 || f.deployment != "deployment" {
		t.Fatal("OCR was silently treated as CPU extraction")
	}
}
