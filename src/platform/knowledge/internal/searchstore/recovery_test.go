package searchstore

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"knowledge-platform/internal/indexing"
)

// Persist writes in the HTTP double before losing the receipt. Recovery uses
// the real Client.Bulk encoder and response validator, with stable document IDs.
func TestBulkPartialWriteAndLostHTTPReply(t *testing.T) {
	for _, mode := range []string{"partial_items", "lost_http_reply"} {
		t.Run(mode, func(t *testing.T) {
			v := testVersion()
			name, _, _ := names(v.ID)
			var mu sync.Mutex
			calls := 0
			var original []byte
			stored := map[string]json.RawMessage{}
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				mu.Lock()
				defer mu.Unlock()
				calls++
				body, e := io.ReadAll(r.Body)
				if e != nil {
					t.Error(e)
					http.Error(w, "read failed", 400)
					return
				}
				if calls == 1 {
					original = append([]byte(nil), body...)
				} else if !bytes.Equal(original, body) {
					t.Error("retry changed NDJSON")
				}
				lines := strings.Split(strings.TrimSpace(string(body)), "\n")
				if len(lines) != 4 {
					t.Error("expected two documents")
					http.Error(w, "invalid bulk", 400)
					return
				}
				items := []any{}
				partial := mode == "partial_items" && calls == 1
				for i, id := range []string{"a", "b"} {
					var action map[string]map[string]any
					if e = json.Unmarshal([]byte(lines[2*i]), &action); e != nil || action["index"]["_id"] != id {
						t.Error("document ID changed")
						http.Error(w, "invalid action", 400)
						return
					}
					row := map[string]any{"_id": id, "_index": name, "status": 201, "_shards": map[string]int{"total": 2, "successful": 2, "failed": 0}}
					if partial && i == 1 {
						row["status"] = 429
						row["error"] = map[string]string{"type": "es_rejected_execution_exception"}
					} else {
						stored[id] = json.RawMessage(lines[2*i+1])
					}
					items = append(items, map[string]any{"index": row})
				}
				if mode == "lost_http_reply" && calls == 1 {
					conn, _, e := w.(http.Hijacker).Hijack()
					if e != nil {
						t.Error(e)
						return
					}
					_ = conn.Close()
					return
				}
				_ = json.NewEncoder(w).Encode(map[string]any{"errors": partial, "items": items})
			}))
			defer server.Close()
			c := &Client{Base: server.URL, HTTP: server.Client()}
			rows := []indexing.Record{{ID: "a", Text: "first"}, {ID: "b", Text: "second"}}
			if e := c.Bulk(context.Background(), v, rows); e == nil {
				t.Fatal("unconfirmed batch accepted")
			}
			mu.Lock()
			firstCalls, firstCount := calls, len(stored)
			mu.Unlock()
			wantCount := 1
			if mode == "lost_http_reply" {
				wantCount = 2
			}
			if firstCalls != 1 || firstCount != wantCount {
				t.Fatal("unexpected hidden retry or fixture side effects", firstCalls, firstCount)
			}
			if e := c.Bulk(context.Background(), v, rows); e != nil {
				t.Fatal(e)
			}
			mu.Lock()
			defer mu.Unlock()
			if calls != 2 || len(stored) != 2 {
				t.Fatal("replay duplicated documents", calls, len(stored))
			}
			for _, row := range rows {
				var got indexing.Record
				if e := json.Unmarshal(stored[row.ID], &got); e != nil || got.ID != row.ID || got.Text != row.Text {
					t.Fatal("replayed content changed", e)
				}
			}
		})
	}
}
