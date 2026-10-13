package indexing

import (
	"encoding/json"
	"testing"
)

func TestHNSWInputDefaultsBoundsAndStrictness(t *testing.T) {
	for _, tt := range []struct {
		raw   string
		m, ef int
	}{
		{"", 16, 128}, {`{}`, 16, 128}, {`{"m":32}`, 32, 128}, {`{"ef_construction":256}`, 16, 256},
		{`{"m":2,"ef_construction":16}`, 2, 16}, {`{"m":64,"ef_construction":1024}`, 64, 1024},
	} {
		got, e := ParseHNSW([]byte(tt.raw))
		if e != nil || got.Parameters != (HNSW{tt.m, tt.ef}) {
			t.Fatal(tt.raw, got, e)
		}
	}
	for _, raw := range []string{`null`, `[]`, `1`, `{"m":null}`, `{"m":true}`, `{"m":"32"}`, `{"m":32.0}`, `{"m":3e1}`, `{"m":1}`, `{"m":65}`, `{"ef_construction":15}`, `{"ef_construction":1025}`, `{"ef_search":20}`, `{"engine":"faiss"}`, `{"m":16,"m":32}`, `{"m":{}}`, `{} {}`} {
		if _, e := ParseHNSW([]byte(raw)); e == nil {
			t.Fatal("invalid HNSW accepted", raw)
		}
	}
}
func TestStoredConfigIsCompleteAndImmutable(t *testing.T) {
	c, _ := ParseHNSW([]byte(`{"m":32,"ef_construction":256}`))
	raw, _ := json.Marshal(c)
	var got Config
	if e := json.Unmarshal(raw, &got); e != nil || got != c {
		t.Fatal(got, e)
	}
	for _, raw := range []string{`null`, `{}`, `{"schema_version":"indexing-config-v1","method":"hnsw","engine":"lucene","space_type":"cosinesimil","parameters":{"m":32}}`} {
		if json.Unmarshal([]byte(raw), &got) == nil {
			t.Fatal("incomplete stored config accepted", raw)
		}
	}
	for _, field := range []string{"engine", "method", "space_type", "schema_version"} {
		var body map[string]any
		_ = json.Unmarshal(raw, &body)
		body[field] = "other"
		bad, _ := json.Marshal(body)
		if json.Unmarshal(bad, &got) == nil {
			t.Fatal("unsupported config accepted", field)
		}
	}
}
