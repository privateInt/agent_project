package runtime

import (
	"fmt"
	"reflect"
	"strings"
)

// Gemma's saved tokenizer may mark an existing token (e.g. <mask>) special.
// Preserve that flag and the trainer's BOS/EOS insertion options by using the
// saved tokenizer. Token identities and tokenizer.json encode rules stay fixed.
func compatibleSavedTokenizer(baseDir, sourceDir string, rows int64) error {
	base, err := tokenizerJSON(baseDir, "tokenizer.json")
	if err != nil { return err }
	saved, err := tokenizerJSON(sourceDir, "tokenizer.json")
	if err != nil { return fmt.Errorf("complete saved tokenizer.json required: %w", err) }
	for _, key := range []string{"model", "normalizer", "pre_tokenizer", "post_processor", "decoder", "padding", "truncation"} {
		if !reflect.DeepEqual(base[key], saved[key]) { return fmt.Errorf("unsupported saved tokenizer change: %s", key) }
	}
	left, err := addedTokenRecords(base["added_tokens"], rows)
	if err != nil { return err }
	right, err := addedTokenRecords(saved["added_tokens"], rows)
	if err != nil { return err }
	if !reflect.DeepEqual(left, right) { return fmt.Errorf("saved tokenizer changed token IDs, content or encoding flags") }
	model, ok := saved["model"].(map[string]any)
	if !ok { return fmt.Errorf("saved tokenizer has no vocabulary model") }
	switch vocab := model["vocab"].(type) {
	case map[string]any:
		if len(vocab) == 0 { return fmt.Errorf("empty tokenizer vocabulary") }
		for _, id := range vocab { if _, err := tokenID(id, rows); err != nil { return err } }
	case []any: // Unigram vocabulary IDs are positions in this array.
		if len(vocab) == 0 || int64(len(vocab)) > rows { return fmt.Errorf("tokenizer vocabulary exceeds saved embeddings") }
	default:
		return fmt.Errorf("unsupported tokenizer vocabulary layout")
	}
	baseConfig, err := tokenizerJSON(baseDir, "tokenizer_config.json")
	if err != nil { return err }
	savedConfig, err := tokenizerJSON(sourceDir, "tokenizer_config.json")
	if err != nil { return fmt.Errorf("complete saved tokenizer_config.json required: %w", err) }
	for _, key := range []string{"bos_token", "eos_token", "pad_token", "unk_token"} {
		a, b := baseConfig[key], savedConfig[key]
		if strings.HasSuffix(key, "_token") { a, b = tokenContent(a), tokenContent(b) }
		if !reflect.DeepEqual(a, b) { return fmt.Errorf("unsupported saved tokenizer config change: %s", key) }
	}
	// The entire saved config is copied unchanged into adapter-base. These
	// options therefore need not match the original base tokenizer. Requiring
	// equality also incorrectly rejects a missing key vs an explicit boolean.
	// Keep omission as omission; do not guess a default or coerce strings/null.
	for _, key := range []string{"add_bos_token", "add_eos_token"} {
		if value, present := savedConfig[key]; present {
			if _, ok := value.(bool); !ok {
				return fmt.Errorf("saved tokenizer config %s must be a boolean when present", key)
			}
		}
	}
	return nil
}

func tokenID(value any, rows int64) (int64, error) {
	id, ok := value.(float64)
	if !ok || id < 0 || id >= float64(rows) || id != float64(int64(id)) { return 0, fmt.Errorf("token ID is outside saved embeddings or is not an integer") }
	return int64(id), nil
}

func addedTokenRecords(value any, rows int64) (map[int64]map[string]any, error) {
	list, ok := value.([]any)
	if !ok { return nil, fmt.Errorf("saved tokenizer needs an added_tokens array") }
	result := map[int64]map[string]any{}
	for _, value := range list {
		record, ok := value.(map[string]any)
		if !ok { return nil, fmt.Errorf("invalid added token record") }
		id, err := tokenID(record["id"], rows)
		if err != nil { return nil, err }
		if _, exists := result[id]; exists { return nil, fmt.Errorf("duplicate added token ID") }
		if _, ok := record["special"].(bool); !ok { return nil, fmt.Errorf("added token special flag must be boolean") }
		copy := map[string]any{}
		for key, value := range record { if key != "special" { copy[key] = value } }
		result[id] = copy
	}
	return result, nil
}
