package runtime

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "fmt"
    "os"
    "path/filepath"

    "github.com/google/uuid"
    "model-platform/internal/embedding"
)

const EmbeddingProfile = embedding.ProfileID
const EmbeddingRepository = embedding.Repository

// ValidateEmbeddingSnapshot reads only authenticated metadata/tokenizer files,
// not multi-GB weights on each inference. Prepare already verifies every weight.
func ValidateEmbeddingSnapshot(root string, p Plan) (string, error) {
    id, err := uuid.Parse(p.DeploymentID)
    if err != nil || id == uuid.Nil || id.String() != p.DeploymentID || p.Source.Adapter != nil {
        return "", fmt.Errorf("embedding requires a canonical snapshot deployment")
    }
    dir := filepath.Join(root, p.DeploymentID, "model")
    for _, name := range []string{root, filepath.Join(root, p.DeploymentID), dir} {
        info, e := os.Lstat(name)
        if e != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 { return "", fmt.Errorf("unsafe embedding cache directory") }
    }
    manifestPath := filepath.Join(dir, "_manifest.json")
    info, err := os.Lstat(manifestPath)
    if err != nil || !info.Mode().IsRegular() || info.Size() > 16<<20 { return "", fmt.Errorf("embedding manifest unavailable") }
    raw, err := os.ReadFile(manifestPath)
    if err != nil { return "", err }
    manifest, err := parseSnapshot(raw, p.Source)
    if err != nil { return "", err }
    files := map[string]snapshotFile{}
    for _, f := range manifest.Files { files[f.Path] = f }
    read := func(name string, dest any) error {
        f, ok := files[name]
        limit := int64(1<<20)
        if dest == nil { limit = 128<<20 }
        if !ok || f.Size <= 0 || f.Size > limit { return fmt.Errorf("embedding metadata missing/oversized: %s", name) }
        filename := filepath.Join(dir, filepath.FromSlash(name))
        for parent := filepath.Dir(filename); parent != filepath.Dir(dir); parent = filepath.Dir(parent) {
            info, e := os.Lstat(parent)
            if e != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 { return fmt.Errorf("unsafe embedding metadata directory") }
        }
        if e := checkFile(filename, f); e != nil { return e }
        if dest != nil {
            data, e := os.ReadFile(filename)
            if e != nil { return e }
            if e = json.Unmarshal(data, dest); e != nil { return fmt.Errorf("invalid embedding metadata: %s", name) }
        }
        return nil
    }
    var config struct {
        ModelType string `json:"model_type"`
        Architectures []string `json:"architectures"`
        HiddenSize int `json:"hidden_size"`
        MaxPosition int `json:"max_position_embeddings"`
        VocabSize int `json:"vocab_size"`
    }
    if err = read("config.json", &config); err != nil { return "", err }
    if config.ModelType != "xlm-roberta" || len(config.Architectures) != 1 || config.Architectures[0] != "XLMRobertaModel" || config.HiddenSize != 1024 || config.MaxPosition != 8194 || config.VocabSize != 250002 {
        return "", fmt.Errorf("snapshot architecture does not match snowflake-ko-v1")
    }
    var modules []struct { Index int `json:"idx"`; Path string `json:"path"`; Type string `json:"type"` }
    if err = read("modules.json", &modules); err != nil { return "", err }
    types := []string{"Transformer", "Pooling", "Normalize"}
    paths := []string{"", "1_Pooling", "2_Normalize"}
    if len(modules) != 3 { return "", fmt.Errorf("unsupported embedding modules") }
    for i, m := range modules {
        if m.Index != i || m.Type != "sentence_transformers.models."+types[i] || m.Path != paths[i] { return "", fmt.Errorf("unsupported embedding module") }
    }
    var pooling map[string]any
    if err = read("1_Pooling/config.json", &pooling); err != nil { return "", err }
    expectedPooling := map[string]any{"word_embedding_dimension":float64(1024), "pooling_mode_cls_token":true,
        "pooling_mode_mean_tokens":false, "pooling_mode_max_tokens":false, "pooling_mode_mean_sqrt_len_tokens":false,
        "pooling_mode_weightedmean_tokens":false, "pooling_mode_lasttoken":false, "include_prompt":true}
    if len(pooling) != len(expectedPooling) { return "", fmt.Errorf("unsupported embedding pooling fields") }
    for name, expected := range expectedPooling {
        if pooling[name] != expected { return "", fmt.Errorf("unsupported embedding pooling: %s", name) }
    }
    var prompts struct { Prompts map[string]string `json:"prompts"`; Default *string `json:"default_prompt_name"`; Similarity string `json:"similarity_fn_name"` }
    if err = read("config_sentence_transformers.json", &prompts); err != nil { return "", err }
    if len(prompts.Prompts) != 1 || prompts.Prompts["query"] != "query: " || prompts.Default != nil || prompts.Similarity != "cosine" { return "", fmt.Errorf("unsupported embedding prompts") }
    hashes := map[string]string{}
    for _, name := range []string{"tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "sentencepiece.bpe.model", "added_tokens.json"} {
        if _, exists := files[name]; !exists && name != "tokenizer.json" && name != "tokenizer_config.json" { continue }
        if err = read(name, nil); err != nil { return "", err }
        hashes[name] = files[name].SHA256
    }
    data, _ := json.Marshal(hashes)
    digest := sha256.Sum256(data)
    return hex.EncodeToString(digest[:]), nil
}
