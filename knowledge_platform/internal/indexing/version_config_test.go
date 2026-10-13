package indexing

import (
	"encoding/json"
	"testing"
)

func TestLegacyVersionConfigAndInvalidStoredValues(t *testing.T) {
	var v Version
	if e := json.Unmarshal([]byte(`{"index_version_id":"legacy"}`), &v); e != nil || v.IndexingConfig == nil || *v.IndexingConfig != DefaultConfig() {
		t.Fatal(v, e)
	}
	c, _ := ParseHNSW([]byte(`{"m":32}`))
	v.IndexingConfig = &c
	raw, _ := json.Marshal(v)
	var again Version
	if e := json.Unmarshal(raw, &again); e != nil || *again.IndexingConfig != c {
		t.Fatal(again, e)
	}
	for _, raw := range []string{`{"indexing_config":null}`, `{"indexing_config":{}}`, `{"indexing_config":false}`} {
		if json.Unmarshal([]byte(raw), &v) == nil {
			t.Fatal("invalid config silently defaulted", raw)
		}
	}
}
