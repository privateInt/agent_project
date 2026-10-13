package main

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"knowledge-platform/internal/searchstore"
)

func TestObserverOnlyReadsExactVersionAndChecksAbsenceOwnershipAndCount(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	name := "knowledge-v1-" + strings.ReplaceAll(id, "-", "")
	for _, tc := range []struct {
		name, state string
		code        int
		owner       string
		count       int
		fail        bool
	}{
		{"held", "held", 200, "knowledge-platform", 2, false},
		{"released", "released", 404, "", 0, false},
		{"still exists", "released", 200, "knowledge-platform", 2, true},
		{"missing held", "held", 404, "", 0, true},
		{"unauthorized is not missing", "released", 403, "", 0, true},
		{"other owner", "held", 200, "other", 2, true},
		{"wrong count", "held", 200, "knowledge-platform", 1, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				if r.Method != "GET" {
					t.Errorf("mutation: %s", r.Method)
				}
				user, password, ok := r.BasicAuth()
				if !ok || user != "test" || password != "private" {
					t.Error("missing auth")
				}
				if r.URL.Path != "/"+name+"/_mapping" && r.URL.Path != "/"+name+"/_count" {
					t.Errorf("unexpected path: %s", r.URL.Path)
				}
				if tc.code != 200 {
					w.WriteHeader(tc.code)
					return
				}
				if strings.HasSuffix(r.URL.Path, "/_mapping") {
					fmt.Fprintf(w, `{"%s":{"mappings":{"_meta":{"owner":"%s","index_version_id":"%s"}}}}`, name, tc.owner, id)
				} else {
					fmt.Fprintf(w, `{"count":%d,"_shards":{"total":2,"successful":2,"failed":0}}`, tc.count)
				}
			}))
			defer s.Close()
			c := &searchstore.Client{Base: s.URL, User: "test", Password: "private", HTTP: s.Client()}
			err := observe(context.Background(), c, expected{ID: id, State: tc.state, Count: 2})
			if (err != nil) != tc.fail {
				t.Fatalf("error=%v want failure=%v", err, tc.fail)
			}
		})
	}
}
