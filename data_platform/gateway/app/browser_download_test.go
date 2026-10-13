package app

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	"encoding/hex"
	"google.golang.org/grpc"
	"io"
	"net/http/httptest"
	"testing"
)

type browserDownloadClient struct {
	collectionv1.CollectionServiceClient
	corrupt  bool
	response bool
}

func (f *browserDownloadClient) GetDocument(context.Context, *collectionv1.GetDocumentRequest, ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	sum := sha256.Sum256([]byte("raw"))
	return &collectionv1.SourceDocument{DocumentId: "doc", SizeBytes: 99, Sha256: "DOM-hash", BrowserCapture: &collectionv1.BrowserCapture{Representation: "rendered_dom", ResponseSha256: hex.EncodeToString(sum[:]), ResponseSizeBytes: 3}}, nil
}

type browserDownloadStream struct {
	grpc.ClientStream
	sent bool
	body []byte
}

func (s *browserDownloadStream) Recv() (*collectionv1.DocumentChunk, error) {
	if s.sent {
		return nil, io.EOF
	}
	s.sent = true
	return &collectionv1.DocumentChunk{Data: s.body}, nil
}
func (f *browserDownloadClient) ReadDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (collectionv1.CollectionService_ReadDocumentClient, error) {
	f.response = r.HttpResponse
	body := []byte("raw")
	if f.corrupt {
		body = []byte("bad")
	}
	return &browserDownloadStream{body: body}, nil
}
func TestBrowserHTTPResponseDownloadUsesIndependentIntegrity(t *testing.T) {
	for _, corrupt := range []bool{false, true} {
		client := &browserDownloadClient{corrupt: corrupt}
		w := httptest.NewRecorder()
		New(client, nil).Handler().ServeHTTP(w, httptest.NewRequest("GET", "/api/source-documents/doc/http-response", nil))
		if !client.response {
			t.Fatal("raw selector not passed to domain")
		}
		if corrupt {
			if w.Code != 502 || w.Body.String() == "bad" {
				t.Fatal("corrupt raw response exposed")
			}
		} else if w.Code != 200 || w.Body.String() != "raw" || w.Header().Get("Content-Type") != "application/octet-stream" {
			t.Fatal(w.Code, w.Body.String())
		}
	}
}
