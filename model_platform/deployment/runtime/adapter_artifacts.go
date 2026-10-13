package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"model-platform/internal/artifacts"
	"model-platform/internal/storage"
)

type adapterConfig struct {
	PEFTType         string                     `json:"peft_type"`
	TaskType         string                     `json:"task_type"`
	Rank             int                        `json:"r"`
	Alpha            float64                    `json:"lora_alpha"`
	Bias             string                     `json:"bias"`
	DoRA             bool                       `json:"use_dora"`
	ModulesToSave    []string                   `json:"modules_to_save"`
	RankPattern      map[string]json.RawMessage `json:"rank_pattern"`
	AlphaPattern     map[string]json.RawMessage `json:"alpha_pattern"`
	LoRABias         bool                       `json:"lora_bias"`
	TargetParameters []string                   `json:"target_parameters"`
}

func readSmall(name string, limit int64) ([]byte, error) {
	info, err := os.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !info.Mode().IsRegular() || info.Size() > limit {
		return nil, fmt.Errorf("invalid metadata file: %s", filepath.Base(name))
	}
	return os.ReadFile(name)
}

func parseAdapterConfig(data []byte) (adapterConfig, error) {
	var c adapterConfig
	if err := json.Unmarshal(data, &c); err != nil {
		return c, fmt.Errorf("invalid adapter_config.json")
	}
	if c.PEFTType != "LORA" || c.TaskType != "CAUSAL_LM" || c.Rank < 1 || c.Rank > MaxLoRARank || c.Alpha <= 0 ||
		(c.Bias != "" && c.Bias != "none") || c.DoRA || c.LoRABias || len(c.ModulesToSave) > 0 ||
		len(c.RankPattern) > 0 || len(c.AlphaPattern) > 0 || len(c.TargetParameters) > 0 {
		return c, fmt.Errorf("unsupported adapter: requires causal LoRA rank 1..%d, no DoRA, bias, modules_to_save or per-module patterns", MaxLoRARank)
	}
	return c, nil
}

// The full immutable training manifest remains the authority. Download only
// root inference files; optimizer state and intermediate checkpoints are unused.
func adapterFiles(data []byte, source *AdapterSource) ([]artifacts.File, error) {
	m, digest, err := artifacts.Parse(data)
	if err != nil {
		return nil, err
	}
	if !SupportedAdapterServingFormat(source.ServingFormat) || digest != source.SHA256 || m.TotalBytes != source.SizeBytes || int64(m.FileCount) != source.FileCount {
		return nil, fmt.Errorf("adapter manifest differs from registered metadata")
	}
	var files []artifacts.File
	for _, f := range m.Files {
		switch f.Path {
		case "adapter_config.json", "adapter_model.safetensors", "tokenizer.json", "tokenizer_config.json", "chat_template.jinja":
			files = append(files, f)
		case "special_tokens_map.json", "added_tokens.json":
			if source.ServingFormat == 2 { files = append(files, f) }
		}
	}
	return files, nil
}

func validateAdapterSource(ctx context.Context, dir string, source *AdapterSource) error {
	data, err := readSmall(filepath.Join(dir, artifacts.ManifestName), 16<<20)
	if err != nil {
		return err
	}
	files, err := adapterFiles(data, source)
	if err != nil {
		return err
	}
	allowed := map[string]bool{artifacts.ManifestName: true}
	for _, f := range files {
		if err := ctx.Err(); err != nil {
			return err
		}
		allowed[f.Path] = true
		if err := checkFile(filepath.Join(dir, f.Path), snapshotFile{Path: f.Path, Size: f.Size, SHA256: f.SHA256}); err != nil {
			return err
		}
	}
	entries, err := os.ReadDir(dir)
	if err != nil {
		return err
	}
	for _, entry := range entries {
		if !allowed[entry.Name()] || entry.IsDir() || entry.Type()&os.ModeSymlink != 0 {
			return fmt.Errorf("unexpected adapter cache entry")
		}
	}
	return nil
}

func (f SnapshotFetcher) prepareAdapter(ctx context.Context, p Plan) error {
	source := p.Source.Adapter
	if source == nil || !SupportedAdapterServingFormat(source.ServingFormat) {
		return fmt.Errorf("unsupported adapter serving format")
	}
	bucket, prefix, err := storage.ParseS3URI(source.URI)
	if err != nil || bucket != f.Store.Bucket() || !strings.HasSuffix(prefix, "/") {
		return fmt.Errorf("adapter is outside the configured artifact bucket/prefix")
	}
	dir := filepath.Join(f.Root, p.DeploymentID, "adapter-source")
	if info, err := os.Lstat(dir); err == nil {
		if !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return fmt.Errorf("unsafe adapter cache directory")
		}
	} else if !os.IsNotExist(err) {
		return err
	} else {
		temp, err := os.MkdirTemp(filepath.Dir(dir), ".adapter-source-")
		if err != nil {
			return err
		}
		defer os.RemoveAll(temp)
		manifest := filepath.Join(temp, artifacts.ManifestName)
		if _, err := f.Store.DownloadObject(ctx, prefix+artifacts.ManifestName, manifest); err != nil {
			return err
		}
		data, err := readSmall(manifest, 16<<20)
		if err != nil {
			return err
		}
		files, err := adapterFiles(data, source)
		if err != nil {
			return err
		}
		for _, entry := range files {
			if err := ctx.Err(); err != nil {
				return err
			}
			if _, err := f.Store.DownloadObject(ctx, prefix+entry.Path, filepath.Join(temp, entry.Path)); err != nil {
				return err
			}
		}
		if err := validateAdapterSource(ctx, temp, source); err != nil {
			return err
		}
		if err := os.Rename(temp, dir); err != nil {
			return err
		}
	}
	if err := validateAdapterSource(ctx, dir, source); err != nil {
		return err
	}
	baseDir := filepath.Join(f.Root, p.DeploymentID, "model")
	config, err := readSmall(filepath.Join(dir, "adapter_config.json"), 1<<20)
	if err != nil {
		return err
	}
	cfg, err := parseAdapterConfig(config)
	if err != nil {
		return err
	}
	if source.ServingFormat == 2 {
		prepared := filepath.Join(f.Root, p.DeploymentID, p.ModelDirectory())
		if err := prepareAdapterBase(ctx, baseDir, dir, prepared, p, cfg); err != nil { return err }
		baseDir = prepared
	} else if err := sameTokenizer(baseDir, dir); err != nil {
		return err
	}
	// Build a private serving projection. Source MinIO objects are never edited.
	return projectAdapter(ctx, baseDir, dir, filepath.Join(f.Root, p.DeploymentID, "adapter"), p, cfg, config)
}
