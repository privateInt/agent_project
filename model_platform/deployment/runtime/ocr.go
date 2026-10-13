package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

const SuryaProfile = "surya-ocr-v2"
const SuryaRepository = "datalab-to/surya-ocr-2"
const SuryaImage = "vllm/vllm-openai:v0.20.1"

func validateSuryaSnapshot(directory string) error {
	path := filepath.Join(directory, "config.json")
	info, e := os.Stat(path)
	if e != nil || info.Size() > 1<<20 {
		return fmt.Errorf("OCR model config unavailable or oversized")
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		return e
	}
	var config struct {
		ModelType     string   `json:"model_type"`
		Architectures []string `json:"architectures"`
	}
	if json.Unmarshal(raw, &config) != nil || config.ModelType != "qwen3_5" || len(config.Architectures) != 1 || config.Architectures[0] != "Qwen3_5ForConditionalGeneration" {
		return fmt.Errorf("Surya snapshot architecture is incompatible with this profile")
	}
	for _, name := range []string{"tokenizer.json", "preprocessor_config.json", "processor_config.json", "chat_template.jinja"} {
		info, e := os.Stat(filepath.Join(directory, name))
		if e != nil || !info.Mode().IsRegular() || info.Size() == 0 {
			return fmt.Errorf("OCR snapshot requires its tokenizer, processor and chat template")
		}
	}
	return nil
}

func ValidateModelProfile(profile, modelType, repo, kind string) error {
	if profile == RerankerProfile {
        if (modelType != "reranker" && modelType != "llm") || repo != RerankerRepository || kind != "snapshot" { return fmt.Errorf("ko-reranker-v1 requires its supported reranker snapshot") }
        return nil
    }
    if profile == "" && repo == RerankerRepository { return fmt.Errorf("ko-reranker requires explicit ko-reranker-v1 serving profile") }
	if profile == EmbeddingProfile {
        if (modelType != "embedding" && modelType != "llm") || repo != EmbeddingRepository || kind != "snapshot" {
            return fmt.Errorf("snowflake-ko-v1 requires its supported embedding snapshot")
        }
        return nil
    }
	if profile == SuryaProfile {
		if modelType != "ocr" || repo != SuryaRepository || kind != "snapshot" {
			return fmt.Errorf("Surya profile requires an OCR model with the datalab-to/surya-ocr-2 snapshot")
		}
		return nil
	}
	if profile != "" || modelType != "llm" {
		return fmt.Errorf("default vLLM profile requires an LLM model")
	}
	return nil
}

// Existing drivers retain their default image contract. A profile must never
// silently fall back to the older LLM image when mixed binaries are running.
func ResolveImage(ctx context.Context, driver Driver, profile string) (string, error) {
	if profile == "" || profile == EmbeddingProfile || profile == RerankerProfile {
		return driver.Image(ctx)
	}
	if profile != SuryaProfile {
		return "", fmt.Errorf("unsupported serving profile")
	}
	resolver, ok := driver.(interface {
		ProfileImage(context.Context, string) (string, error)
	})
	if !ok {
		return "", fmt.Errorf("runtime driver does not support the OCR profile")
	}
	return resolver.ProfileImage(ctx, profile)
}

func (d Docker) ProfileImage(ctx context.Context, profile string) (string, error) {
	if profile != SuryaProfile {
		return "", fmt.Errorf("unsupported serving profile")
	}
	cfg := d.Config
	cfg.Image = cfg.OCRImage
	if cfg.Image == "" {
		cfg.Image = SuryaImage
	}
	if err := cfg.Validate(); err != nil {
		return "", err
	}
	return (Docker{Config: cfg}).Image(ctx)
}
