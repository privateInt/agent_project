// Package runtime manages Deployment-owned serving containers, not execution Jobs.
package runtime

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"math"
	"regexp"
	"strings"

	deploymentv1 "model-platform/gen/deployment/v1"
	"model-platform/internal/execution"
)

type Spec struct {
	Engine               string  `json:"engine"`
	Profile              string  `json:"profile,omitempty"`
	MaxModelLen          int32   `json:"max_model_len,omitempty"`
	GPUMemoryUtilization float64 `json:"gpu_memory_utilization,omitempty"`
}

func Normalize(req *deploymentv1.ServingSpec) (Spec, error) {
	engine := strings.ToLower(strings.TrimSpace(req.GetEngine()))
	if engine == "" {
		engine = "demo"
	}
	profile := strings.TrimSpace(req.GetProfile())
	if profile != "" && (engine != "vllm" || (profile != SuryaProfile && profile != EmbeddingProfile && profile != RerankerProfile)) {
		return Spec{}, fmt.Errorf("unsupported serving profile")
	}
	if engine == "demo" {
		if req.GetMaxModelLen() != 0 || req.GetGpuMemoryUtilization() != 0 {
			return Spec{}, fmt.Errorf("demo does not accept serving options")
		}
		return Spec{Engine: "demo"}, nil
	}
	if engine != "vllm" {
		return Spec{}, fmt.Errorf("serving.engine must be demo or vllm")
	}
	length, memory := req.GetMaxModelLen(), req.GetGpuMemoryUtilization()
	if length == 0 {
		length = 2048
        if profile == RerankerProfile { length = 512 }
		if profile == SuryaProfile {
			length = 18000
		}
	}
	if profile == RerankerProfile && length != 512 { return Spec{}, fmt.Errorf("ko-reranker-v1 requires max_model_len = 512") }
	if profile == SuryaProfile && length < 18000 {
		return Spec{}, fmt.Errorf("Surya requires max_model_len >= 18000")
	}
	if profile == EmbeddingProfile && length > 8192 {
        return Spec{}, fmt.Errorf("snowflake-ko-v1 max_model_len must be <= 8192")
    }
	if memory == 0 {
		memory = 0.5
	}
	if length < 128 || length > 32768 {
		return Spec{}, fmt.Errorf("max_model_len must be 128..32768")
	}
	if math.IsNaN(memory) || math.IsInf(memory, 0) || memory < 0.1 || memory > 0.95 {
		return Spec{}, fmt.Errorf("gpu_memory_utilization must be 0.1..0.95")
	}
	return Spec{Engine: engine, Profile: profile, MaxModelLen: length, GPUMemoryUtilization: memory}, nil
}
func (s Spec) Proto() *deploymentv1.ServingSpec {
	return &deploymentv1.ServingSpec{Engine: s.Engine, Profile: s.Profile, MaxModelLen: s.MaxModelLen, GpuMemoryUtilization: s.GPUMemoryUtilization}
}

type Config struct {
	Image, Network, Volume, CacheDir, NodeName string
	OCRImage                                   string
}

func (c Config) Validate() error {
	name := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.-]*$`)
	if !name.MatchString(c.Network) || !name.MatchString(c.Volume) || c.CacheDir == "" {
		return fmt.Errorf("invalid serving network, cache volume or directory")
	}
	image := c.Image[strings.LastIndex(c.Image, "/")+1:]
	tagged := regexp.MustCompile(`^[A-Za-z0-9_.-]+:[A-Za-z0-9_][A-Za-z0-9_.-]*$`).MatchString(image) && !strings.HasSuffix(image, ":latest")
	pinned := regexp.MustCompile(`^[A-Za-z0-9_.:-]+@sha256:[a-f0-9]{64}$`).MatchString(image)
	if strings.TrimSpace(c.Image) != c.Image || (!tagged && !pinned) {
		return fmt.Errorf("serving image must use an explicit version or digest")
	}
	return nil
}

type Source struct {
	ModelVersionID string `json:"model_version_id"`
	URI            string `json:"uri"`
	SHA256         string `json:"sha256"`
	SizeBytes      int64  `json:"size_bytes"`
	// Omit for snapshots: existing persisted plan hashes must remain identical.
	Adapter *AdapterSource `json:"adapter,omitempty"`
}

type AdapterSource struct {
	ServingFormat  int    `json:"serving_format"`
	ModelVersionID string `json:"model_version_id"`
	TrainingRunID  string `json:"training_run_id"`
	URI            string `json:"uri"`
	SHA256         string `json:"sha256"`
	SizeBytes      int64  `json:"size_bytes"`
	FileCount      int64  `json:"file_count"`
}

const MaxLoRARank = 64
const CurrentAdapterServingFormat = 2

func SupportedAdapterServingFormat(format int) bool { return format == 1 || format == 2 }

type Plan struct {
	DeploymentID string              `json:"deployment_id"`
	OperationID  string              `json:"operation_id"`
	ImageID      string              `json:"image_id"`
	Spec         Spec                `json:"spec"`
	Resources    execution.Resources `json:"resources"`
	Source       Source              `json:"source"`
}

func (p Plan) Name() string      { return "model-serving-" + p.DeploymentID }
func (p Plan) ModelName() string { return "deployment-" + p.DeploymentID }
func (p Plan) ModelDirectory() string {
	if p.Source.Adapter != nil && p.Source.Adapter.ServingFormat == 2 {
		return "adapter-base"
	}
	return "model"
}
func (p Plan) BaseModelName() string {
	if p.Source.Adapter != nil {
		return p.ModelName() + "-base"
	}
	return p.ModelName()
}
func (p Plan) Hash() string {
	data, _ := json.Marshal(p)
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}
