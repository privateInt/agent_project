package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"

	"model-platform/internal/artifacts"
)

// Format 2 gives each adapter a private base. Saved full embeddings are loaded
// as ordinary model weights, never delegated to vLLM's LoRA tensor loader.
// No LoRA merge, new embedding initialization, truncation or dtype conversion.
func prepareAdapterBase(ctx context.Context, baseDir, sourceDir, target string, p Plan, cfg adapterConfig) error {
	base, err := baseTensors(baseDir)
	if err != nil { return err }
	all, err := tensorHeader(filepath.Join(sourceDir, "adapter_model.safetensors"))
	if err != nil { return err }
	config, err := tokenizerJSON(baseDir, "config.json")
	if err != nil { return err }
	saved := map[string]tensorInfo{}
	for name, tensor := range all {
		plain := strings.ReplaceAll(strings.TrimPrefix(name, "base_model.model."), ".base_layer.weight", ".weight")
		if plain != "model.embed_tokens.weight" && plain != "lm_head.weight" { continue }
		if _, exists := saved[plain]; exists { return fmt.Errorf("duplicate saved embedding identity: %s", plain) }
		saved[plain] = tensor
	}
	preserve := config["model_type"] == "gemma3_text" &&
		reflect.DeepEqual(config["architectures"], []any{"Gemma3ForCausalLM"}) && len(saved) == 2
	var names []string
	var vocab int64
	if preserve {
		input, output := saved["model.embed_tokens.weight"], saved["lm_head.weight"]
		original, ok := base["model.embed_tokens.weight"]
		if !ok || len(original.Shape) != 2 || len(input.Shape) != 2 || !reflect.DeepEqual(input.Shape, output.Shape) ||
			input.Shape[1] != original.Shape[1] || input.Shape[0] < original.Shape[0] ||
			config["vocab_size"] != float64(original.Shape[0]) || config["hidden_size"] != float64(input.Shape[1]) {
			return fmt.Errorf("saved Gemma embeddings must preserve hidden size and supply both complete vocabulary matrices")
		}
		if config["quantization_config"] != nil || config["text_config"] != nil {
			return fmt.Errorf("saved embedding preparation requires an unquantized Gemma3ForCausalLM snapshot")
		}
		vocab = input.Shape[0]
		if err := compatibleSavedTokenizer(baseDir, sourceDir, vocab); err != nil { return err }
		for name, tensor := range saved { base[name] = tensor; names = append(names, name) }
		sort.Strings(names)
		config["vocab_size"] = vocab
		// Both complete matrices were saved. Do not let a tied-weight loader
		// discard a potentially different trained lm_head.
		config["tie_word_embeddings"] = false
	} else {
		// Preserve format 1's support for other models and adapters without
		// both full matrices. This is not a generic changed-tokenizer bypass.
		if err := sameTokenizer(baseDir, sourceDir); err != nil { return err }
		if _, _, err := selectedLoRA(ctx, baseDir, all, cfg); err != nil { return err }
	}
	temp, err := os.MkdirTemp(filepath.Dir(target), ".adapter-base-")
	if err != nil { return err }
	defer os.RemoveAll(temp)
	encoded, err := json.Marshal(config)
	if err != nil { return err }
	if err := os.WriteFile(filepath.Join(temp, "config.json"), encoded, 0o644); err != nil { return err }
	keys := make([]string, 0, len(base))
	for key := range base { keys = append(keys, key) }
	sort.Strings(keys)
	if err := writeProjection(ctx, filepath.Join(temp, "model.safetensors"), base, keys); err != nil { return err }
	// The prepared model has one newly indexed weight file. Never copy the
	// original shard index, weights or unneeded repository code into this view.
	for _, name := range []string{"generation_config.json", "tokenizer.json", "tokenizer_config.json", "tokenizer.model", "vocab.json", "merges.txt", "special_tokens_map.json", "added_tokens.json", "chat_template.jinja"} {
		dir := baseDir
		if preserve && name != "generation_config.json" && name != "tokenizer.model" && name != "vocab.json" && name != "merges.txt" {
			dir = sourceDir
		}
		// The complete saved fast tokenizer is authoritative when embeddings
		// are restored. Omit base slow-tokenizer sidecars in that mode.
		if preserve && (name == "tokenizer.model" || name == "vocab.json" || name == "merges.txt") { continue }
		data, err := readSmall(filepath.Join(dir, name), 64<<20)
		if os.IsNotExist(err) { continue }
		if err != nil { return err }
		if err := os.WriteFile(filepath.Join(temp, name), data, 0o644); err != nil { return err }
	}
	// Validate every LoRA pair and every removed saved embedding against the
	// model actually supplied to vLLM, before publishing the prepared directory.
	if _, _, err := selectedLoRA(ctx, temp, all, cfg); err != nil { return err }
	receipt := struct {
		Format int `json:"serving_format"`
		PlanSHA string `json:"serving_plan_sha256"`
		BaseSHA string `json:"base_manifest_sha256"`
		AdapterSHA string `json:"adapter_manifest_sha256"`
		Preserved []string `json:"preserved_embeddings"`
		Vocab int64 `json:"restored_vocab_size,omitempty"`
		Tokenizer string `json:"tokenizer_source"`
		Files []snapshotFile `json:"files"`
	}{Format: 2, PlanSHA: p.Hash(), BaseSHA: p.Source.SHA256, AdapterSHA: p.Source.Adapter.SHA256, Preserved: names, Vocab: vocab, Tokenizer: "base"}
	if preserve { receipt.Tokenizer = "adapter" }
	entries, err := os.ReadDir(temp)
	if err != nil { return err }
	for _, entry := range entries {
		if err := ctx.Err(); err != nil { return err }
		hash, size, err := artifacts.HashFile(filepath.Join(temp, entry.Name()))
		if err != nil { return err }
		receipt.Files = append(receipt.Files, snapshotFile{Path: entry.Name(), SHA256: hash, Size: size})
	}
	data, err := json.Marshal(receipt)
	if err != nil { return err }
	if err := os.WriteFile(filepath.Join(temp, "serving-base.json"), data, 0o644); err != nil { return err }
	return publishAdapterBase(ctx, temp, target)
}

// Compare against freshly derived bytes, not a self-asserted cache receipt.
// Refuse corruption; never overwrite a directory that may be in use.
func publishAdapterBase(ctx context.Context, temp, target string) error {
	info, err := os.Lstat(target)
	if os.IsNotExist(err) {
		if err := ctx.Err(); err != nil { return err }
		return os.Rename(temp, target)
	}
	if err != nil { return err }
	if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 { return fmt.Errorf("unsafe prepared base directory") }
	want, err := os.ReadDir(temp)
	if err != nil { return err }
	got, err := os.ReadDir(target)
	if err != nil { return err }
	if len(want) != len(got) { return fmt.Errorf("unexpected prepared base files") }
	for i, entry := range want {
		if err := ctx.Err(); err != nil { return err }
		if entry.Name() != got[i].Name() { return fmt.Errorf("unexpected prepared base file") }
		hash, size, err := artifacts.HashFile(filepath.Join(temp, entry.Name()))
		if err != nil { return err }
		if err := checkFile(filepath.Join(target, entry.Name()), snapshotFile{Path: entry.Name(), SHA256: hash, Size: size}); err != nil { return err }
	}
	return nil
}
