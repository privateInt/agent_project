package app

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"encoding/json"
	"google.golang.org/grpc"
	"net/http/httptest"
	"strings"
	"testing"
)

type fakeCollection struct {
	collectionv1.CollectionServiceClient
	creates int
	key     string
}

func (f *fakeCollection) CreateCrawl(_ context.Context, r *collectionv1.CreateCrawlRequest, _ ...grpc.CallOption) (*collectionv1.CrawlAccepted, error) {
	f.creates++
	f.key = r.IdempotencyKey
	return &collectionv1.CrawlAccepted{CrawlRunId: "run", OperationId: "op"}, nil
}
func (f *fakeCollection) ListCrawls(context.Context, *collectionv1.ListCrawlsRequest, ...grpc.CallOption) (*collectionv1.ListCrawlsResponse, error) {
	return &collectionv1.ListCrawlsResponse{}, nil
}
func TestStrictCreateFieldsAndDuplicateKeys(t *testing.T) {
	for _, body := range []string{`null`, `{"Name":"x","url":"https://example.com/"}`, `{"name":"x","name":"y","url":"https://example.com/"}`, `{"name":"x","url":"https://example.com/","options":{"max_bytes":1,"max_bytes":2}}`, `{"name":"x","url":"https://example.com/","options":{"timeout_seconds":null}}`, `{"name":"x","url":"https://example.com/","idempotency_key":"body-key"}`, `{"name":"x","url":"https://example.com/","options":{"browser":true}}`} {
		if _, e := decodeCreate([]byte(body)); e == nil {
			t.Fatal("invalid body accepted", body)
		}
	}
	if _, e := decodeCreate([]byte(`{"name":"x","url":"https://example.com/","options":{"max_bytes":1024,"timeout_seconds":30}}`)); e != nil {
		t.Fatal(e)
	}
}
func TestBrowsingNeverCreatesAndPostRequiresHeader(t *testing.T) {
	f := &fakeCollection{}
	handler := New(f, nil).Handler()
	for _, path := range []string{"/health", "/api/crawl-runs"} {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest("GET", path, nil))
		if w.Code != 200 {
			t.Fatal(w.Code, w.Body.String())
		}
	}
	body := `{"name":"x","url":"https://example.com/"}`
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, httptest.NewRequest("POST", "/api/crawl-runs", strings.NewReader(body)))
	if w.Code != 400 || f.creates != 0 {
		t.Fatal("mutation without explicit idempotency key")
	}
	r := httptest.NewRequest("POST", "/api/crawl-runs", strings.NewReader(body))
	r.Header.Set("Idempotency-Key", "test-one")
	w = httptest.NewRecorder()
	handler.ServeHTTP(w, r)
	if w.Code != 202 || f.creates != 1 || f.key != "test-one" {
		t.Fatal(w.Code, f)
	}
}
func TestPublicOperationHasNoExecutionGrant(t *testing.T) {
	raw, _ := json.Marshal(operationView(&operationv1.Operation{OperationId: "op", DispatchToken: "secret-grant", DispatchOwner: "private-owner", ExecutionInstanceId: "private-instance"}))
	for _, secret := range []string{"secret-grant", "private-owner", "private-instance", "dispatch_token", "execution_instance_id"} {
		if strings.Contains(string(raw), secret) {
			t.Fatal("execution grant exposed")
		}
	}
}

func TestStrictExpansionOptionsAndManifestView(t *testing.T) {
	r, e := decodeCreate([]byte(`{"name":"x","url":"https://example.com/","options":{"follow_links":true,"include_attachments":true,"max_depth":2,"max_documents":5,"max_total_bytes":16777216}}`))
	if e != nil || !r.Options.FollowLinks || !r.Options.IncludeAttachments || r.Options.MaxDepth != 2 || r.Options.MaxDocuments != 5 {
		t.Fatal(r, e)
	}
	for _, opts := range []string{`{"follow_links":"true"}`, `{"include_attachments":1}`, `{"max_depth":1.5}`, `{"max_documents":null}`, `{"follow_links":true,"follow_links":false}`, `{"browser":true}`} {
		if _, e := decodeCreate([]byte(`{"name":"x","url":"https://example.com/","options":` + opts + `}`)); e == nil {
			t.Fatal(opts)
		}
	}
	v := crawlView(&collectionv1.CrawlRun{OutputDocumentId: "seed", OutputDocumentIds: []string{"seed", "child"}, DocumentLimitReached: true})
	if len(v["output_document_ids"].([]string)) != 2 || v["document_limit_reached"] != true {
		t.Fatal(v)
	}
	if len(sourceIDs(&collectionv1.CrawlRun{})) != 0 || len(sourceIDs(&collectionv1.CrawlRun{OutputDocumentId: "old"})) != 1 {
		t.Fatal("legacy view")
	}
}

func TestStrictBrowserOptionsAndCaptureView(t *testing.T) {
	good := `{"name":"x","url":"https://example.com/","options":{"render_javascript":true,"render_wait_ms":1500}}`
	r, e := decodeCreate([]byte(good))
	if e != nil || !r.Options.RenderJavascript || r.Options.RenderWaitMs != 1500 {
		t.Fatal(r, e)
	}
	for _, opts := range []string{`{"render_javascript":1}`, `{"render_javascript":null}`, `{"render_wait_ms":"1000"}`, `{"render_wait_ms":1.2}`, `{"render_javascript":true,"render_javascript":false}`} {
		if _, e := decodeCreate([]byte(`{"name":"x","url":"https://example.com/","options":` + opts + `}`)); e == nil {
			t.Fatal("invalid browser option accepted")
		}
	}
	raw, _ := json.Marshal(documentView(&collectionv1.SourceDocument{BrowserCapture: &collectionv1.BrowserCapture{Representation: "rendered_dom", ResponseSha256: "digest"}}))
	if !strings.Contains(string(raw), `"representation":"rendered_dom"`) || !strings.Contains(string(raw), `"response_sha256":"digest"`) {
		t.Fatal("capture hidden from public view")
	}
}
