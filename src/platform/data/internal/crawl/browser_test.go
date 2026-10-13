package crawl

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"errors"
	"os"
	"strings"
	"testing"
)

func TestBrowserOptionsRequireExplicitOptInAndBoundedWait(t *testing.T) {
	for _, o := range []Options{{RenderWaitMS: 1}, {RenderJavaScript: true, RenderWaitMS: -1}, {RenderJavaScript: true, RenderWaitMS: 10001}} {
		if _, e := NormalizeOptions(o); e == nil {
			t.Fatal(o)
		}
	}
	o, e := NormalizeOptions(Options{RenderJavaScript: true})
	if e != nil || o.RenderWaitMS != 1000 || o.MaxDocuments != 1 || o.MaxTotalBytes != 16<<20 || o.ExecutionType() != "collection.browser" {
		t.Fatal(o, e)
	}
	if OptionsFromProto(o.Proto()) != o {
		t.Fatal("options lost")
	}
}
func TestRenderedLinksPreserveRawAndCleanBothFiles(t *testing.T) {
	calls := []string{}
	f := site(map[string]page{"/": {body: "<html>initial</html>", kind: "text/html"}, "/new.txt": {body: "file", kind: "text/plain"}}, &calls)
	o, _ := NormalizeOptions(Options{RenderJavaScript: true, IncludeAttachments: true, MaxDocuments: 2})
	paths := []string{}
	render := func(ctx context.Context, f *Fetcher, seed string, o Options, raw Result) (Result, *collectionv1.BrowserCapture, error) {
		paths = append(paths, raw.Path)
		file, e := os.CreateTemp("", "dom-test-*")
		if e != nil {
			return Result{}, nil, e
		}
		body := `<html>rendered<a href="/new.txt">dynamic</a></html>`
		file.WriteString(body)
		file.Close()
		paths = append(paths, file.Name())
		return Result{Path: file.Name(), FinalURL: raw.FinalURL, ContentType: "text/html; charset=utf-8", Size: int64(len(body)), Status: 200}, &collectionv1.BrowserCapture{Representation: "rendered_dom"}, nil
	}
	seen := 0
	batch, e := f.CollectRendered(context.Background(), "https://example.com/", o, func(ctx context.Context, item Item) error {
		seen++
		if item.Role == "seed" {
			raw, _ := os.ReadFile(item.Response.Path)
			if string(raw) != "<html>initial</html>" || item.Capture == nil {
				t.Fatal("raw response changed")
			}
		}
		return nil
	}, render)
	if e != nil || seen != 2 || len(batch.Items) != 2 || !strings.HasSuffix(calls[1], "/new.txt") {
		t.Fatal(batch, e, calls)
	}
	for _, p := range paths {
		if _, e := os.Stat(p); !os.IsNotExist(e) {
			t.Fatal("temporary response or DOM leaked")
		}
	}
	if _, e = f.Collect(context.Background(), "https://example.com/", o, nil); e == nil {
		t.Fatal("browser silently downgraded to HTTP")
	}
}
func TestRenderOrSinkFailureDoesNotReturnSuccessfulBatch(t *testing.T) {
	for _, renderFails := range []bool{true, false} {
		calls := []string{}
		f := site(map[string]page{"/": {body: "<html>initial</html>", kind: "text/html"}}, &calls)
		o, _ := NormalizeOptions(Options{RenderJavaScript: true})
		paths := []string{}
		render := func(ctx context.Context, f *Fetcher, seed string, o Options, raw Result) (Result, *collectionv1.BrowserCapture, error) {
			paths = append(paths, raw.Path)
			if renderFails {
				return Result{}, nil, errors.New("render failed")
			}
			file, _ := os.CreateTemp("", "dom-test-*")
			file.WriteString("<html>DOM</html>")
			file.Close()
			paths = append(paths, file.Name())
			raw.Path = file.Name()
			raw.Size = 16
			return raw, nil, nil
		}
		batch, e := f.CollectRendered(context.Background(), "https://example.com/", o, func(context.Context, Item) error { return errors.New("sink failed") }, render)
		if e == nil || len(batch.Items) != 0 {
			t.Fatal("failed capture accepted")
		}
		for _, p := range paths {
			if _, e := os.Stat(p); !os.IsNotExist(e) {
				t.Fatal("temporary file leaked")
			}
		}
	}
}
