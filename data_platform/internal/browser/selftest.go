package browser

import (
	"context"
	"data-platform/internal/crawl"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
)

type fixtureTransport func(*http.Request) (*http.Response, error)

func (f fixtureTransport) RoundTrip(r *http.Request) (*http.Response, error) { return f(r) }

// SelfTest uses the actual Go -> isolated Python -> Chromium -> Go resource
// bridge. All remote bytes are authored fixtures; no external network is used.
func SelfTest(r *Renderer) error {
	calls := []string{}
	f := crawl.NewFetcher()
	f.Client.Transport = fixtureTransport(func(req *http.Request) (*http.Response, error) {
		calls = append(calls, req.URL.Path)
		if req.Header.Get("Cookie") != "" || req.Header.Get("Authorization") != "" {
			return nil, fmt.Errorf("unexpected credentials")
		}
		body, kind := "", "text/html; charset=utf-8"
		switch req.URL.Path {
		case "/":
			body = `<!doctype html><html><head><link rel="icon" href="data:,"></head><body><p id="value">initial</p><script src="/app.js"></script></body></html>`
		case "/app.js":
			kind = "text/javascript"
			body = `fetch('/value').then(r=>r.text()).then(t=>{document.querySelector('#value').textContent=t;const a=document.createElement('a');a.href='/attachment.txt';a.textContent='attachment';document.body.append(a);});fetch('http://127.0.0.1/private').catch(()=>{});fetch('https://other.example/blocked').catch(()=>{});`
		case "/value":
			kind = "text/plain"
			body = "BROWSER_PIPE_OK"
		case "/attachment.txt":
			kind = "text/plain"
			body = "attachment bytes"
		default:
			return nil, fmt.Errorf("unexpected fixture route")
		}
		return &http.Response{StatusCode: 200, Header: http.Header{"Content-Type": {kind}}, Body: io.NopCloser(strings.NewReader(body)), ContentLength: int64(len(body)), Request: req}, nil
	})
	o, _ := crawl.NormalizeOptions(crawl.Options{RenderJavaScript: true, IncludeAttachments: true, MaxDocuments: 2, RenderWaitMS: 250, TimeoutSeconds: 30})
	seen := 0
	batch, e := f.CollectRendered(context.Background(), "https://example.com/", o, func(_ context.Context, item crawl.Item) error {
		seen++
		body, e := os.ReadFile(item.Path)
		if e != nil {
			return e
		}
		if item.Role == "seed" {
			if item.Response == nil || item.Capture == nil || !strings.Contains(string(body), ">BROWSER_PIPE_OK<") || item.SHA256 == item.Response.SHA256 || item.Capture.BlockedRequests < 2 {
				return fmt.Errorf("DOM, raw response or network policy mismatch")
			}
			raw, e := os.ReadFile(item.Response.Path)
			if e != nil || strings.Contains(string(raw), "BROWSER_PIPE_OK") {
				return fmt.Errorf("initial response changed")
			}
		} else if item.Capture != nil || item.Response != nil || string(body) != "attachment bytes" {
			return fmt.Errorf("attachment was rendered or changed")
		}
		return nil
	}, r.Render)
	if e != nil {
		return e
	}
	if seen != 2 || len(batch.Items) != 2 || strings.Join(calls, ",") != "/,/app.js,/value,/attachment.txt" {
		return fmt.Errorf("dynamic discovery or pipe routing failed")
	}
	return nil
}
