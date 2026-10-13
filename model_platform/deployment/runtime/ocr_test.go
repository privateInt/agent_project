package runtime

import (
	"encoding/json"
	deploymentv1 "model-platform/gen/deployment/v1"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSuryaProfileDefaultsAndArtifactBoundary(t *testing.T) {
	spec, e := Normalize(&deploymentv1.ServingSpec{Engine: "vllm", Profile: SuryaProfile})
	if e != nil || spec.MaxModelLen != 18000 || spec.Proto().Profile != SuryaProfile {
		t.Fatal(spec, e)
	}
	for _, request := range []*deploymentv1.ServingSpec{{Engine: "demo", Profile: SuryaProfile}, {Engine: "vllm", Profile: "unknown"}, {Engine: "vllm", Profile: SuryaProfile, MaxModelLen: 2048}} {
		if _, e := Normalize(request); e == nil {
			t.Fatal("invalid profile accepted")
		}
	}
	if e := ValidateModelProfile(SuryaProfile, "ocr", SuryaRepository, "snapshot"); e != nil {
		t.Fatal(e)
	}
	for _, values := range [][4]string{{SuryaProfile, "llm", SuryaRepository, "snapshot"}, {SuryaProfile, "ocr", "other/repo", "snapshot"}, {SuryaProfile, "ocr", SuryaRepository, "adapter"}, {"", "ocr", SuryaRepository, "snapshot"}} {
		if ValidateModelProfile(values[0], values[1], values[2], values[3]) == nil {
			t.Fatal("incompatible artifact accepted")
		}
	}
}

func TestSuryaRuntimeKeepsLLMPlanStableAndBoundsImages(t *testing.T) {
	plan := testPlan()
	raw, _ := json.Marshal(plan)
	if strings.Contains(string(raw), "profile") {
		t.Fatal("legacy plan serialization changed")
	}
	before := plan.Hash()
	plan.Spec.Profile = SuryaProfile
	plan.Spec.MaxModelLen = 18000
	if plan.Hash() == before {
		t.Fatal("OCR profile missing from runtime identity")
	}
	args, e := CreateArgs(Config{Network: "ops-infra", Volume: "cache"}, plan)
	if e != nil {
		t.Fatal(e)
	}
	text := strings.Join(args, " ")
	for _, required := range []string{"--limit-mm-per-prompt", `{"image":1}`, "--max-num-seqs 4", "HF_HUB_OFFLINE=1"} {
		if !strings.Contains(text, required) {
			t.Fatal("missing OCR runtime constraint", required)
		}
	}
	if strings.Contains(text, "--trust-remote-code") {
		t.Fatal("remote model code enabled")
	}
}

func TestSuryaSnapshotRequiresItsVisionProcessor(t *testing.T) {
	dir := t.TempDir()
	for _, name := range []string{"tokenizer.json", "preprocessor_config.json", "processor_config.json", "chat_template.jinja"} {
		if e := os.WriteFile(filepath.Join(dir, name), []byte("fixture"), 0600); e != nil {
			t.Fatal(e)
		}
	}
	path := filepath.Join(dir, "config.json")
	os.WriteFile(path, []byte(`{"model_type":"qwen3_5","architectures":["Qwen3_5ForConditionalGeneration"]}`), 0600)
	if e := validateSuryaSnapshot(dir); e != nil {
		t.Fatal(e)
	}
	os.Remove(filepath.Join(dir, "processor_config.json"))
	if validateSuryaSnapshot(dir) == nil {
		t.Fatal("missing vision processor accepted")
	}
	os.WriteFile(path, []byte(`{"model_type":"gemma3","architectures":["Gemma3ForCausalLM"]}`), 0600)
	if validateSuryaSnapshot(dir) == nil {
		t.Fatal("text-only architecture accepted")
	}
}
