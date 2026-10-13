package searchstore

import (
	"context"
	"encoding/json"
	"knowledge-platform/internal/indexing"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHNSWMappingOwnershipForLegacyAndCustom(t *testing.T) {
	legacy := testVersion()
	defaults := indexing.DefaultConfig()
	explicit := legacy
	explicit.IndexingConfig = &defaults
	if !sameMapping(Mapping(legacy), Mapping(explicit)) {
		t.Fatal("legacy mapping changed")
	}
	config, _ := indexing.ParseHNSW([]byte(`{"m":32,"ef_construction":256}`))
	custom := legacy
	custom.IndexingConfig = &config
	if sameMapping(Mapping(legacy), Mapping(custom)) {
		t.Fatal("custom mapping ignored")
	}
	method := Mapping(custom)["properties"].(map[string]any)["vector"].(map[string]any)["method"].(map[string]any)
	if method["parameters"].(map[string]any)["m"] != 32 || method["parameters"].(map[string]any)["ef_construction"] != 256 {
		t.Fatal(method)
	}
	name, alias, _ := names(custom.ID)
	mapping := Mapping(custom)
	deletes := 0
	requests := 0
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		requests++
		if r.Method == "DELETE" {
			deletes++
			t.Error("mismatch must not delete")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{name: map[string]any{"mappings": mapping, "aliases": map[string]any{alias: map[string]any{}}, "settings": map[string]any{"index": map[string]string{"knn": "true"}}}})
	}))
	defer srv.Close()
	c := &Client{Base: srv.URL, HTTP: srv.Client()}
	if e := c.Ensure(context.Background(), custom); e != nil {
		t.Fatal(e)
	}
	mapping = Mapping(legacy)
	if c.Ensure(context.Background(), custom) == nil || c.Delete(context.Background(), custom) == nil || deletes != 0 {
		t.Fatal("mismatched mapping accepted")
	}
	before := requests
	bad := custom
	invalid := config
	invalid.Parameters.M = 0
	bad.IndexingConfig = &invalid
	if c.Ensure(context.Background(), bad) == nil || requests != before {
		t.Fatal("invalid config contacted storage")
	}
}
