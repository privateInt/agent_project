package app

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestConsoleInventoryOnlyForwardsBoundedReads(t *testing.T) {
	id := "10000000-0000-4000-8000-000000000001"
	var paths []string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" {
			t.Error("model lifecycle mutation escaped")
		}
		paths = append(paths, r.URL.RequestURI())
		_, _ = io.WriteString(w, `{"items":[]}`)
	}))
	defer upstream.Close()
	proxy, e := NewModelConsole(upstream.URL)
	if e != nil {
		t.Fatal(e)
	}
	handler := (&Server{ModelConsole: proxy}).Handler()
	call := func(method, path string) int {
		w := httptest.NewRecorder()
		handler.ServeHTTP(w, httptest.NewRequest(method, path, nil))
		return w.Code
	}
	for _, path := range []string{"/console/model/deployments?limit=20&cursor=abc", "/console/model/models/" + id, "/console/model/model-versions/" + id} {
		if got := call("GET", path); got != 200 {
			t.Fatal(path, got)
		}
	}
	if paths[0] != "/api/deployments?cursor=abc&limit=20&status=ready" {
		t.Fatal("ready filter or cursor lost", paths)
	}
	if paths[1] != "/api/models/"+id || paths[2] != "/api/model-versions/"+id {
		t.Fatal(paths)
	}
	calls := len(paths)
	for _, path := range []string{"/console/model/deployments?limit=21", "/console/model/deployments?cursor=a&cursor=b", "/console/model/deployments?status=stopped", "/console/model/deployments?url=http://other", "/console/model/deployments?cursor=" + strings.Repeat("a", 2049), "/console/model/models/not-a-uuid", "/console/model/models/00000000-0000-0000-0000-000000000000", "/console/model/models/" + id + "?url=http://other"} {
		if got := call("GET", path); got != 400 {
			t.Fatal("invalid read accepted", path, got)
		}
	}
	for _, path := range []string{"/console/model/deployments", "/console/model/models/" + id, "/console/model/model-versions/" + id, "/console/model/deployments/" + id + "/stop"} {
		if got := call("POST", path); got < 400 {
			t.Fatal("mutation route exposed", path, got)
		}
	}
	if len(paths) != calls {
		t.Fatal("invalid calls reached Model")
	}
}

func TestConsoleInventoryRejectsRedirectAndInvalidUpstream(t *testing.T) {
	for _, mode := range []string{"redirect", "invalid", "oversize"} {
		t.Run(mode, func(t *testing.T) {
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				switch mode {
				case "redirect":
					http.Redirect(w, r, "http://unrelated.invalid/", 302)
				case "invalid":
					_, _ = io.WriteString(w, "not json")
				case "oversize":
					_, _ = io.WriteString(w, `"`+strings.Repeat("a", 1<<20)+`"`)
				}
			}))
			defer upstream.Close()
			proxy, e := NewModelConsole(upstream.URL)
			if e != nil {
				t.Fatal(e)
			}
			w := httptest.NewRecorder()
			(&Server{ModelConsole: proxy}).Handler().ServeHTTP(w, httptest.NewRequest("GET", "/console/model/deployments", nil))
			want := 502
			if mode == "redirect" {
				want = 503
			}
			if w.Code != want {
				t.Fatal(w.Code, w.Body.String())
			}
		})
	}
}
