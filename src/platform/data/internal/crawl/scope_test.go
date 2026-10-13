package crawl

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"reflect"
	"strings"
	"testing"
)

func TestScopeOptionsPreserveLegacyFingerprint(t *testing.T) {
	old, _ := json.Marshal(struct {
		MaxBytes       int64 `json:"max_bytes"`
		TimeoutSeconds int32 `json:"timeout_seconds"`
	}{8 << 20, 120})
	o, e := NormalizeOptions(Options{})
	if e != nil {
		t.Fatal(e)
	}
	encoded, _ := json.Marshal(o)
	if string(old) != string(encoded) {
		t.Fatal("legacy request fingerprint changed")
	}
	for _, o := range []Options{{MaxDocuments: 1}, {FollowLinks: true, MaxDepth: 4}, {IncludeAttachments: true, MaxDepth: 1}, {FollowLinks: true, MaxDocuments: 21}, {FollowLinks: true, MaxTotalBytes: 1}, {FollowLinks: true, MaxTotalBytes: 65 << 20}} {
		if _, e := NormalizeOptions(o); e == nil {
			t.Fatal("invalid scope", o)
		}
	}
}
func TestDiscoverScopeBaseCharsetAndNonNavigationResources(t *testing.T) {
	body := `<base href="/docs/"><script>"<a href='/evil'>"</script><img src="image.png"><form action="/post"></form>
 <a href="a.html#x">A</a><a href="a.html#y">duplicate</a><a href="x.pdf?rev=2&amp;x=3">PDF</a>
 <a href="/download?id=7" download>file</a><a href="https://sub.example.com/a">no</a><a href="http://example.com/a">no</a>
 <a href="https://evil.example/a">no</a><a href="http://127.0.0.1/a">no</a><a href="javascript:alert(1)">no</a><a href="/code.js">no</a>`
	got, limited, e := Discover(strings.NewReader(body), "text/html; charset=utf-8", "https://example.com/index", "https://example.com/")
	want := []Link{{"https://example.com/docs/a.html", false}, {"https://example.com/docs/x.pdf?rev=2&x=3", true}, {"https://example.com/download?id=7", true}}
	if e != nil || limited || !reflect.DeepEqual(got, want) {
		t.Fatal(got, limited, e)
	}
	got, _, e = Discover(strings.NewReader("<a href='/caf\xe9'>x</a>"), "text/html; charset=windows-1252", "https://example.com/", "https://example.com/")
	if e != nil || len(got) != 1 || got[0].URL != "https://example.com/caf%C3%A9" {
		t.Fatal("charset link", got, e)
	}
	got, _, e = Discover(strings.NewReader(`<base href="https://other.example/"><a href="x">x</a>`), "text/html", "https://example.com/", "https://example.com/")
	if e != nil || len(got) != 0 {
		t.Fatal("foreign base escaped scope", got, e)
	}
}
func TestDiscoverBoundsLinksAndTokenMemory(t *testing.T) {
	body := strings.Repeat(`<a href="/x">x</a>`, MaxPageLinks+1)
	_, limited, e := Discover(strings.NewReader(body), "text/html", "https://example.com/", "https://example.com/")
	if e != nil || !limited {
		t.Fatal(limited, e)
	}
	_, _, e = Discover(strings.NewReader(`<a href="`+strings.Repeat("a", (1<<20)+1)+`">`), "text/html", "https://example.com/", "https://example.com/")
	if e == nil {
		t.Fatal("unbounded token")
	}
}

type page struct {
	body, kind, location string
	code                 int
}

func site(pages map[string]page, calls *[]string) *Fetcher {
	f := NewFetcher()
	f.Client.Transport = transport(func(r *http.Request) (*http.Response, error) {
		*calls = append(*calls, r.URL.String())
		p, ok := pages[r.URL.Path]
		if !ok {
			return nil, errors.New("unexpected URL")
		}
		code := p.code
		if code == 0 {
			code = 200
		}
		header := http.Header{"Content-Type": {p.kind}}
		if p.location != "" {
			header.Set("Location", p.location)
		}
		return &http.Response{StatusCode: code, Header: header, Request: r, ContentLength: int64(len(p.body)), Body: io.NopCloser(strings.NewReader(p.body))}, nil
	})
	return f
}
func TestCollectBFSAttachmentsProvenanceAndCleanup(t *testing.T) {
	pages := map[string]page{
		"/":           {body: `<a href="/a">a</a><a href="/a#again">a</a><a href="/manual.pdf">pdf</a><a href="https://outside.example/x">offsite</a>`, kind: "text/html"},
		"/a":          {body: `<a href="/">cycle</a><a href="/deep">deep</a><a href="/data.xlsx">sheet</a>`, kind: "text/html"},
		"/manual.pdf": {body: "pdf", kind: "application/pdf"}, "/data.xlsx": {body: "sheet", kind: "application/octet-stream"},
	}
	calls := []string{}
	paths := []string{}
	f := site(pages, &calls)
	b, e := f.Collect(context.Background(), "https://example.com/", Options{FollowLinks: true, IncludeAttachments: true}, func(_ context.Context, item Item) error {
		paths = append(paths, item.Path)
		_, e := os.ReadFile(item.Path)
		return e
	})
	if e != nil || len(b.Items) != 4 || !b.DepthLimitReached || b.DocumentLimitReached {
		t.Fatal(b, e)
	}
	if !reflect.DeepEqual(calls, []string{"https://example.com/", "https://example.com/a", "https://example.com/manual.pdf", "https://example.com/data.xlsx"}) {
		t.Fatal(calls)
	}
	if b.Items[3].ParentURL != "https://example.com/a" || b.Items[3].Depth != 2 || b.Items[3].Role != "attachment" {
		t.Fatal("provenance", b.Items[3])
	}
	for _, p := range paths {
		if _, e := os.Stat(p); !os.IsNotExist(e) {
			t.Fatal("temporary source retained", p)
		}
	}
}
func TestCollectAttachmentOnlyAndDocumentCap(t *testing.T) {
	pages := map[string]page{"/": {body: `<a href="/a">page</a><a href="/x.pdf">file</a><a href="/y.pdf">file</a>`, kind: "text/html"}, "/x.pdf": {body: "pdf", kind: "application/pdf"}}
	calls := []string{}
	b, e := site(pages, &calls).Collect(context.Background(), "https://example.com/", Options{IncludeAttachments: true, MaxDocuments: 2}, nil)
	if e != nil || len(b.Items) != 2 || !b.DocumentLimitReached || b.DepthLimitReached || calls[1] != "https://example.com/x.pdf" {
		t.Fatal(b, calls, e)
	}
}
func TestCollectRejectsRedirectBeforeForeignRequest(t *testing.T) {
	for _, target := range []string{"https://other.example/x", "http://example.com/x", "https://127.0.0.1/"} {
		calls := []string{}
		f := site(map[string]page{"/": {code: 302, location: target}}, &calls)
		if _, e := f.Collect(context.Background(), "https://example.com/", Options{FollowLinks: true}, nil); e == nil || len(calls) != 1 {
			t.Fatal(target, calls, e)
		}
	}
}
func TestCollectFailureByteBudgetAndCancellation(t *testing.T) {
	for _, mode := range []string{"fetch", "store", "bytes", "cancel"} {
		t.Run(mode, func(t *testing.T) {
			body := `<a href="/x">x</a>`
			calls := []string{}
			pages := map[string]page{"/": {body: body, kind: "text/html"}, "/x": {body: strings.Repeat("a", 20), kind: "text/html"}}
			if mode == "fetch" {
				pages["/x"] = page{code: 429}
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			if mode == "cancel" {
				cancel()
			}
			o := Options{FollowLinks: true}
			if mode == "bytes" {
				o.MaxBytes = int64(len(body))
				o.MaxTotalBytes = o.MaxBytes
			}
			paths := []string{}
			_, e := site(pages, &calls).Collect(ctx, "https://example.com/", o, func(_ context.Context, item Item) error {
				paths = append(paths, item.Path)
				if mode == "store" {
					return errors.New("upload failed")
				}
				return nil
			})
			if e == nil {
				t.Fatal("failure was hidden")
			}
			for _, p := range paths {
				if _, e := os.Stat(p); !os.IsNotExist(e) {
					t.Fatal("failed temporary retained")
				}
			}
		})
	}
}
