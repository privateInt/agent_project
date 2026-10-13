package indexing

import (
	"encoding/json"
	"fmt"
	"knowledge-platform/internal/chunking"
)

// These are platform resource limits, not OpenSearch's complete supported range.
const MinM = 2
const MaxM = 64
const MinEFConstruction = 16
const MaxEFConstruction = 1024

type HNSW struct {
	M              int `json:"m"`
	EFConstruction int `json:"ef_construction"`
}
type Config struct {
	SchemaVersion string `json:"schema_version"`
	Method        string `json:"method"`
	Engine        string `json:"engine"`
	SpaceType     string `json:"space_type"`
	Parameters    HNSW   `json:"parameters"`
}

func DefaultConfig() Config {
	return Config{"indexing-config-v1", "hnsw", "lucene", "cosinesimil", HNSW{16, 128}}
}
func (c Config) Validate() error {
	if c.SchemaVersion != "indexing-config-v1" || c.Method != "hnsw" || c.Engine != "lucene" || c.SpaceType != "cosinesimil" || c.Parameters.M < MinM || c.Parameters.M > MaxM || c.Parameters.EFConstruction < MinEFConstruction || c.Parameters.EFConstruction > MaxEFConstruction {
		return fmt.Errorf("INDEXING_CONFIG_INVALID: require Lucene HNSW cosine, m 2..64 and ef_construction 16..1024")
	}
	return nil
}
func (c *Config) UnmarshalJSON(raw []byte) error {
	type plain Config
	var decoded plain
	if e := chunking.Decode(raw, &decoded); e != nil {
		return fmt.Errorf("INDEXING_CONFIG_INVALID: invalid stored configuration")
	}
	value := Config(decoded)
	if e := value.Validate(); e != nil {
		return e
	}
	*c = value
	return nil
}

// ParseHNSW accepts partial options. Explicit nulls, unknown fields and nonintegers
// are errors; only omitted fields take the existing production defaults.
func ParseHNSW(raw []byte) (Config, error) {
	c := DefaultConfig()
	if len(raw) == 0 {
		return c, nil
	}
	var fields map[string]json.RawMessage
	if chunking.Decode(raw, &fields) != nil || fields == nil {
		return c, fmt.Errorf("HNSW_INVALID: expected an object")
	}
	for k, v := range fields {
		var n *int
		if json.Unmarshal(v, &n) != nil || n == nil {
			return c, fmt.Errorf("HNSW_INVALID: values must be integers")
		}
		switch k {
		case "m":
			c.Parameters.M = *n
		case "ef_construction":
			c.Parameters.EFConstruction = *n
		default:
			return c, fmt.Errorf("HNSW_INVALID: unknown option")
		}
	}
	return c, c.Validate()
}
