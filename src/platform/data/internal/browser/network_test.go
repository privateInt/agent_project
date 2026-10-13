package browser

import (
	"context"
	"data-platform/internal/crawl"
	"io"
	"net/http"
	"strings"
	"testing"
)

func TestBrowserBridgeRejectsPrivateForeignMethodsWithoutNetwork(t *testing.T) {
	f := crawl.NewFetcher()
	calls := 0
	f.Client.Transport = fixtureTransport(func(req *http.Request) (*http.Response, error) { calls++; return nil, nil })
	for _, entry := range [][2]string{{"http://127.0.0.1/", "GET"}, {"http://2130706433/", "GET"}, {"https://other.example/", "GET"}, {"http://example.com/", "GET"}, {"https://example.com/", "POST"}, {"file:///etc/passwd", "GET"}, {"https://example.com:444/", "GET"}} {
		response, e := New().fetch(context.Background(), f, "https://example.com/", entry[0], entry[1], crawl.Options{MaxBytes: 100}, &networkBudget{})
		if e != nil || !response.Blocked || calls != 0 {
			t.Fatal("unchecked request escaped", entry, e)
		}
	}
}
func TestBrowserRedirectHeadersAndBudgets(t *testing.T) {
	o := crawl.Options{MaxBytes: 100}
	for _, location := range []string{"/next.js", "https://other.example/x", "http://example.com/x", "http://127.0.0.1/x"} {
		calls := 0
		f := crawl.NewFetcher()
		f.Client.Transport = fixtureTransport(func(req *http.Request) (*http.Response, error) {
			calls++
			if req.Header.Get("Cookie") != "" || req.Header.Get("Authorization") != "" || req.Header.Get("Accept-Encoding") != "identity" {
				t.Fatal("browser forwarded ambient headers")
			}
			return &http.Response{Request: req, StatusCode: 302, Header: http.Header{"Location": {location}, "Set-Cookie": {"secret=yes"}}, Body: io.NopCloser(strings.NewReader("")), ContentLength: 0}, nil
		})
		r := New()
		p := &networkBudget{}
		response, e := r.fetch(context.Background(), f, "https://example.com/", "https://example.com/x.js", "GET", o, p)
		if e != nil || calls != 1 || p.requests != 1 || response.Headers["set-cookie"] != "" {
			t.Fatal(response, e, calls)
		}
		if location == "/next.js" {
			if response.Headers["location"] != "https://example.com/next.js" || response.Blocked {
				t.Fatal(response)
			}
		} else if !response.Blocked {
			t.Fatal("foreign redirect not blocked")
		}
		p.requests = crawl.BrowserPageRequests
		if _, e = r.fetch(context.Background(), f, "https://example.com/", "https://example.com/x.js", "GET", o, p); e == nil || calls != 1 {
			t.Fatal("request limit ignored")
		}
	}
}
func TestBrowserResourceFailureSizeAndEncoding(t *testing.T) {
	for _, kind := range []string{"status", "truncated", "large", "compressed"} {
		f := crawl.NewFetcher()
		f.Client.Transport = fixtureTransport(func(req *http.Request) (*http.Response, error) {
			resp := &http.Response{Request: req, StatusCode: 200, Header: http.Header{"Content-Type": {"text/plain"}}, ContentLength: 3, Body: io.NopCloser(strings.NewReader("abc"))}
			switch kind {
			case "status":
				resp.StatusCode = 429
			case "truncated":
				resp.ContentLength = 4
			case "large":
				resp.ContentLength = 101
			case "compressed":
				resp.Header.Set("Content-Encoding", "gzip")
			}
			return resp, nil
		})
		if _, e := New().fetch(context.Background(), f, "https://example.com/", "https://example.com/value", "GET", crawl.Options{MaxBytes: 100}, &networkBudget{}); e == nil {
			t.Fatal(kind)
		}
	}
}
