package app

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/blob"
	"data-platform/internal/crawl"
	"encoding/hex"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
)

func testCapture() *collectionv1.BrowserCapture {
	return &collectionv1.BrowserCapture{Representation: "rendered_dom", ResponseSha256: strings.Repeat("a", 64), ResponseSizeBytes: 8, ResponseContentType: "text/html", NetworkRequests: 1, NetworkBytes: 8}
}
func TestBrowserManifestRequiresDOMAndResponseProvenance(t *testing.T) {
	o, _ := crawl.NormalizeOptions(crawl.Options{RenderJavaScript: true, IncludeAttachments: true})
	run := &collectionv1.CrawlRun{Url: "https://example.com/", OperationId: "op", Options: o.Proto()}
	valid := manifest()
	valid.Sources[0].BrowserCapture = testCapture()
	valid.Sources[0].ContentType = "text/html; charset=utf-8"
	if e := validateBatch(run, valid); e != nil {
		t.Fatal(e)
	}
	for _, change := range []func(*collectionv1.CompleteCrawlBatchRequest){
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[0].BrowserCapture = nil },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].BrowserCapture = testCapture() },
		func(r *collectionv1.CompleteCrawlBatchRequest) {
			r.Sources[0].BrowserCapture.ResponseArtifactUri = "s3://arbitrary/object"
		},
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[0].BrowserCapture.ResponseSha256 = "wrong" },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[0].BrowserCapture.NetworkRequests = 129 },
		func(r *collectionv1.CompleteCrawlBatchRequest) {
			r.Sources[0].BrowserCapture.ResponseContentType = "image/png"
		},
	} {
		r := proto.Clone(valid).(*collectionv1.CompleteCrawlBatchRequest)
		change(r)
		if validateBatch(run, r) == nil {
			t.Fatal("invalid capture accepted")
		}
	}
	run.Options.RenderJavascript = false
	run.Options.RenderWaitMs = 0
	if validateBatch(run, valid) == nil {
		t.Fatal("browser result accepted for HTTP request")
	}
}
func TestBrowserResponseVerificationAndReplayAreAtomic(t *testing.T) {
	s, ops, m := fixture(t)
	ctx := context.Background()
	input := request()
	input.Options = &collectionv1.CrawlOptions{RenderJavascript: true}
	old := prepareResultFor(t, s, ops, m, input)
	raw := []byte("<html>initial</html>")
	dom := []byte("<html>rendered</html>")
	digest := func(b []byte) string { sum := sha256.Sum256(b); return hex.EncodeToString(sum[:]) }
	capture := testCapture()
	capture.ResponseSha256 = digest(raw)
	capture.ResponseSizeBytes = int64(len(raw))
	capture.NetworkBytes = int64(len(raw))
	item := &collectionv1.CrawlSourceResult{SourceUrl: input.Url, FinalUrl: input.Url, Role: "seed", Sha256: digest(dom), SizeBytes: int64(len(dom)), ContentType: "text/html; charset=utf-8", HttpStatus: 200, BrowserCapture: capture}
	r := &collectionv1.CompleteCrawlBatchRequest{CrawlRunId: old.CrawlRunId, OperationId: old.OperationId, DispatchToken: old.DispatchToken, ExecutionInstanceId: old.ExecutionInstanceId, ProcessorImageId: old.ProcessorImageId, Sources: []*collectionv1.CrawlSourceResult{item}}
	key, _ := blob.CrawlItemKey(old.CrawlRunId, input.Url, item.Sha256)
	rawKey, _ := blob.BrowserResponseKey(old.CrawlRunId, input.Url, capture.ResponseSha256)
	store := &uploadStore{objects: map[string][]byte{key: dom, rawKey: []byte("tampered")}}
	s.store = store
	if _, e := s.CompleteCrawlBatch(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("corrupt evidence accepted", e)
	}
	var n int
	s.pool.QueryRow(ctx, `SELECT count(*) FROM source_documents`).Scan(&n)
	if n != 0 {
		t.Fatal("partial DOM committed")
	}
	store.objects[rawKey] = raw
	first, e := s.CompleteCrawlBatch(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	second, e := s.CompleteCrawlBatch(ctx, r)
	if e != nil || first.OutputDocumentId != second.OutputDocumentId {
		t.Fatal("completion replay changed", e)
	}
	d, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: first.OutputDocumentId})
	if e != nil || d.ProcessorVersion != crawl.BrowserProcessorVersion || d.BrowserCapture.ResponseArtifactUri != store.URI(rawKey) || d.Sha256 != item.Sha256 {
		t.Fatal(d, e)
	}
	response := &documentStream{}
	if e = s.ReadDocument(&collectionv1.GetDocumentRequest{DocumentId: d.DocumentId, HttpResponse: true}, response); e != nil || string(response.data.Bytes()) != string(raw) {
		t.Fatal("HTTP response read failed", e)
	}
	storedDOM := &documentStream{}
	if e = s.ReadDocument(&collectionv1.GetDocumentRequest{DocumentId: d.DocumentId}, storedDOM); e != nil || string(storedDOM.data.Bytes()) != string(dom) {
		t.Fatal("DOM read differs", e)
	}
	store.objects[rawKey] = []byte("corrupt")
	corrupt := &documentStream{}
	if e = s.ReadDocument(&collectionv1.GetDocumentRequest{DocumentId: d.DocumentId, HttpResponse: true}, corrupt); status.Code(e) != codes.DataLoss || corrupt.sends != 0 {
		t.Fatal("corrupt response streamed", e)
	}
	store.objects[rawKey] = raw
	r.Sources[0].BrowserCapture.BlockedRequests++
	if _, e = s.CompleteCrawlBatch(ctx, r); status.Code(e) != codes.AlreadyExists {
		t.Fatal("browser evidence overwritten", e)
	}
	if e = InitSchema(ctx, s.pool); e != nil {
		t.Fatal("repeat additive migration", e)
	}
}
