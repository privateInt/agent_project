package runtime

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
)

func tokenizerJSON(dir, name string) (map[string]any, error) {
	data, err := readSmall(filepath.Join(dir, name), 64<<20)
	if err != nil {
		return nil, err
	}
	var value map[string]any
	if err := json.Unmarshal(data, &value); err != nil || value == nil {
		return nil, fmt.Errorf("invalid %s", name)
	}
	return value, nil
}

// Tokenization uses the exact base snapshot. A trainer may serialize an
// AddedToken as an object even when the base config uses a plain string.
func tokenContent(value any) any {
	if object, ok := value.(map[string]any); ok {
		return object["content"]
	}
	return value
}

func sameTokenizer(baseDir, adapterDir string) error {
	checks := map[string][]string{
		"tokenizer.json": {"model", "added_tokens", "normalizer", "pre_tokenizer", "post_processor", "decoder"},
		"tokenizer_config.json": {"bos_token", "eos_token", "pad_token", "unk_token", "add_bos_token", "add_eos_token"},
	}
	for name, keys := range checks {
		adapter, err := tokenizerJSON(adapterDir, name)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return err
		}
		base, err := tokenizerJSON(baseDir, name)
		if err != nil {
			return err
		}
		for _, key := range keys {
			a, b := adapter[key], base[key]
			if strings.HasSuffix(key, "_token") {
				a, b = tokenContent(a), tokenContent(b)
			}
			if !reflect.DeepEqual(a, b) {
				return fmt.Errorf("adapter tokenizer changed %s; base-tokenizer serving would change semantics", key)
			}
		}
	}
	return nil
}

// Keep the trainer's chat template when present. Absence permits the base
// template; a malformed or ambiguous saved template must not fall back.
func servingChatTemplate(baseDir, adapterDir string) ([]byte, string, error) {
	for _, source := range []struct{ name, dir string }{{"adapter", adapterDir}, {"base", baseDir}} {
		data, err := readSmall(filepath.Join(source.dir, "chat_template.jinja"), 1<<20)
		if err == nil {
			if strings.TrimSpace(string(data)) == "" {
				return nil, "", fmt.Errorf("empty %s chat template", source.name)
			}
			return data, source.name, nil
		}
		if !os.IsNotExist(err) {
			return nil, "", err
		}
		config, err := tokenizerJSON(source.dir, "tokenizer_config.json")
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, "", err
		}
		value := config["chat_template"]
		if value == nil {
			continue
		}
		template, ok := value.(string)
		if !ok || strings.TrimSpace(template) == "" || len(template) > 1<<20 {
			return nil, "", fmt.Errorf("%s needs one nonempty chat template", source.name)
		}
		return []byte(template), source.name, nil
	}
	return nil, "", fmt.Errorf("adapter chat serving needs a saved or base chat template")
}
