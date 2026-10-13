package indexing

import (
	"encoding/json"
	"fmt"
)

func (v Version) EffectiveConfig() (Config, error) {
	if v.IndexingConfig == nil {
		return DefaultConfig(), nil
	}
	return *v.IndexingConfig, v.IndexingConfig.Validate()
}

// Legacy persisted versions omitted this field and used exactly 16/128.
// Explicit null or invalid configurations must never silently become defaults.
func (v *Version) UnmarshalJSON(raw []byte) error {
	type plain Version
	var decoded plain
	if e := json.Unmarshal(raw, &decoded); e != nil {
		return e
	}
	var fields map[string]json.RawMessage
	if e := json.Unmarshal(raw, &fields); e != nil {
		return e
	}
	if _, present := fields["indexing_config"]; present {
		if decoded.IndexingConfig == nil {
			return fmt.Errorf("INDEXING_CONFIG_INVALID: null configuration")
		}
	} else {
		c := DefaultConfig()
		decoded.IndexingConfig = &c
	}
	*v = Version(decoded)
	return nil
}
