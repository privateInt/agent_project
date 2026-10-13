package runtime

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"model-platform/internal/gpuadmission"
	"net/http"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Evidence struct {
	ID, Name, Image string
	Config          struct{ Labels map[string]string }
	State           struct {
		Status                      string
		Running, Paused, Restarting bool
		OOMKilled                   bool
		ExitCode                    int
	}
	HostConfig      struct{ RestartPolicy struct{ Name string } }
	NetworkSettings struct {
		Networks map[string]json.RawMessage
		Ports    map[string][]struct {
			HostIP   string `json:"HostIp"`
			HostPort string
		}
	}
	Mounts []struct {
		Type, Name, Destination string
		RW                      bool
	}
}

type Driver interface {
	Image(context.Context) (string, error)
	Inspect(context.Context, Plan) (*Evidence, error)
	Create(context.Context, Plan) error
	Start(context.Context, string) error
	Stop(context.Context, string) error
	Healthy(context.Context, Plan) (bool, error)
}

type Docker struct{ Config Config }

func run(ctx context.Context, timeout time.Duration, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	data, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	if err != nil {
		return data, fmt.Errorf("docker %s failed: %s", args[0], strings.TrimSpace(string(data)))
	}
	return data, nil
}

var imageID = regexp.MustCompile(`^sha256:[a-f0-9]{64}$`)
var containerID = regexp.MustCompile(`^[a-f0-9]{64}$`)

func (d Docker) Image(ctx context.Context) (string, error) {
	data, err := run(ctx, 10*time.Second, "image", "inspect", "--format", "{{.Id}}", d.Config.Image)
	if err != nil {
		return "", fmt.Errorf("configured vLLM image is not available locally; pull it before deployment")
	}
	id := strings.TrimSpace(string(data))
	if !imageID.MatchString(id) {
		return "", fmt.Errorf("invalid runtime image identity")
	}
	return id, nil
}

func validateEvidence(p Plan, c Config, e *Evidence) error {
	if !containerID.MatchString(e.ID) || e.Name != "/"+p.Name() || e.Image != p.ImageID || e.Config.Labels["model-platform.owner"] != "deployment-serving" || e.Config.Labels["model-platform.deployment_id"] != p.DeploymentID || e.Config.Labels["model-platform.operation_id"] != p.OperationID || e.Config.Labels["model-platform.serving_plan"] != p.Hash() {
		return fmt.Errorf("serving container identity mismatch; manual review required")
	}
	if e.HostConfig.RestartPolicy.Name != "no" && e.HostConfig.RestartPolicy.Name != "" {
		return fmt.Errorf("unexpected runtime restart policy")
	}
	if _, ok := e.NetworkSettings.Networks[c.Network]; !ok {
		return fmt.Errorf("serving network mismatch")
	}
	for _, mount := range e.Mounts {
		if mount.Type == "volume" && mount.Name == c.Volume && mount.Destination == "/models" && !mount.RW {
			return nil
		}
	}
	return fmt.Errorf("serving cache mount mismatch")
}
func (d Docker) Inspect(ctx context.Context, p Plan) (*Evidence, error) {
	// Never inspect or print Config.Env, which may contain unrelated secrets.
	format := `{"ID":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},"Config":{"Labels":{{json .Config.Labels}}},"State":{{json .State}},"HostConfig":{"RestartPolicy":{{json .HostConfig.RestartPolicy}}},"NetworkSettings":{{json .NetworkSettings}},"Mounts":{{json .Mounts}}}`
	data, err := run(ctx, 10*time.Second, "inspect", "--type", "container", "--format", format, p.Name())
	if err != nil {
		if strings.Contains(string(data), "No such object") || strings.Contains(string(data), "No such container") {
			return nil, nil
		}
		return nil, err
	}
	var e Evidence
	if err := json.Unmarshal(data, &e); err != nil {
		return nil, err
	}
	if err := validateEvidence(p, d.Config, &e); err != nil {
		return nil, err
	}
	return &e, nil
}

func CreateArgs(c Config, p Plan) ([]string, error) {
	if p.Spec.Profile != "" && p.Spec.Profile != SuryaProfile && p.Spec.Profile != EmbeddingProfile && p.Spec.Profile != RerankerProfile {
		return nil, fmt.Errorf("unsupported serving profile")
	}
	if (p.Spec.Profile == SuryaProfile || p.Spec.Profile == EmbeddingProfile || p.Spec.Profile == RerankerProfile) && p.Source.Adapter != nil {
		return nil, fmt.Errorf("selected serving profile requires a snapshot")
	}
	if p.Source.Adapter != nil && !SupportedAdapterServingFormat(p.Source.Adapter.ServingFormat) {
		return nil, fmt.Errorf("unsupported adapter serving format")
	}
	if !imageID.MatchString(p.ImageID) || len(p.Resources.GPUIds) != 1 || p.Resources.GPUCount != 1 {
		return nil, fmt.Errorf("serving requires one exact GPU and a pinned image ID")
	}
	if p.Resources.NodeName != "" && p.Resources.NodeName != c.NodeName {
		return nil, fmt.Errorf("serving node mismatch")
	}
	args := []string{"create", "--name", p.Name(), "--pull", "never", "--restart", "no",
		"--network", c.Network, "--publish", "127.0.0.1::8000", "--shm-size", "1g",
		"--gpus", "device=" + strconv.Itoa(int(p.Resources.GPUIds[0])),
		"--mount", "type=volume,src=" + c.Volume + ",dst=/models,readonly",
		"--label", "model-platform.owner=deployment-serving",
		"--label", "model-platform.deployment_id=" + p.DeploymentID,
		"--label", "model-platform.operation_id=" + p.OperationID,
		"--label", "model-platform.serving_plan=" + p.Hash(),
		"--env", "HF_HUB_OFFLINE=1", "--env", "TRANSFORMERS_OFFLINE=1",
		"--env", "VLLM_NO_USAGE_STATS=1", "--env", "DO_NOT_TRACK=1",
		"--env", "NVIDIA_DRIVER_CAPABILITIES=compute,utility",
		"--log-opt", "max-size=20m", "--log-opt", "max-file=3",
		p.ImageID, "--model", filepath.ToSlash(filepath.Join("/models", p.DeploymentID, p.ModelDirectory())),
		"--served-model-name", p.BaseModelName(), "--host", "0.0.0.0", "--port", "8000",
		"--dtype", "auto", "--max-model-len", strconv.Itoa(int(p.Spec.MaxModelLen)),
		"--gpu-memory-utilization", strconv.FormatFloat(p.Spec.GPUMemoryUtilization, 'f', -1, 64),
		"--tensor-parallel-size", "1", "--enforce-eager"}
	if p.Spec.Profile == RerankerProfile {
        if p.Spec.MaxModelLen != 512 { return nil, fmt.Errorf("reranker requires 512 tokens") }
        args = append(args, "--runner", "pooling", "--pooler-config", `{"use_activation":false}`, "--max-num-seqs", "16")
    }
	if p.Spec.Profile == EmbeddingProfile {
        args = append(args, "--runner", "pooling")
    }
	if p.Spec.Profile == SuryaProfile {
		args = append(args, "--limit-mm-per-prompt", `{"image":1}`, "--mm-processor-kwargs", `{"min_pixels":3136,"max_pixels":6291456}`, "--max-num-seqs", "4", "--max-num-batched-tokens", "8192")
	}
	if p.Source.Adapter != nil {
		args = append(args, "--chat-template", "/models/"+p.DeploymentID+"/adapter/chat_template.jinja")
		module, _ := json.Marshal(struct {
			Name string `json:"name"`
			Path string `json:"path"`
			Base string `json:"base_model_name"`
		}{p.ModelName(), "/models/" + p.DeploymentID + "/adapter", p.BaseModelName()})
		args = append(args, "--enable-lora", "--max-loras", "1", "--max-lora-rank", strconv.Itoa(MaxLoRARank), "--lora-modules", string(module))
	}
	return args, nil
}
func (d Docker) Create(ctx context.Context, p Plan) error {
	args, err := CreateArgs(d.Config, p)
	if err != nil {
		return err
	}
	return gpuadmission.FromEnvironment().Run(ctx, gpuadmission.Request{
		Name: p.Name(), OperationID: p.OperationID, GPUIds: gpuadmission.SortedIDs(p.Resources.GPUIds), GPUCount: p.Resources.GPUCount, Shared: true,
	}, func() error { _, err := run(ctx, 20*time.Second, args...); return err })
}
func (d Docker) Start(ctx context.Context, id string) error {
	if !containerID.MatchString(id) {
		return fmt.Errorf("full container ID required")
	}
	return gpuadmission.Start(ctx, id, func() error { _, err := run(ctx, 20*time.Second, "start", id); return err })
}
func (d Docker) Stop(ctx context.Context, id string) error {
	if !containerID.MatchString(id) {
		return fmt.Errorf("full container ID required")
	}
	_, err := run(ctx, 30*time.Second, "stop", "--time", "20", id)
	return err
}
func (d Docker) ReleaseGPU(ctx context.Context, p Plan) error {
	return gpuadmission.FromEnvironment().ReleaseStopped(ctx, p.Name(), p.OperationID, func(ctx context.Context, id string) error {
		e, err := d.Inspect(ctx, p)
		if err != nil {
			return err
		}
		if e == nil {
			return nil
		}
		if e.ID != id || e.State.Status != "created" || e.State.Running || e.State.Paused || e.State.Restarting {
			return fmt.Errorf("refusing to remove a runtime that may have started")
		}
		// No force: Docker itself rejects a concurrent external start.
		_, err = run(ctx, 10*time.Second, "rm", id)
		return err
	})
}
func (d Docker) Healthy(ctx context.Context, p Plan) (bool, error) {
	base := "http://" + p.Name() + ":8000"
	client := &http.Client{Timeout: 3 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	for _, endpoint := range []string{"/health", "/v1/models"} {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+endpoint, nil)
		if err != nil {
			return false, err
		}
		response, err := client.Do(req)
		if err != nil {
			return false, err
		}
		data, readErr := io.ReadAll(io.LimitReader(response.Body, 1<<20))
		response.Body.Close()
		if readErr != nil || response.StatusCode != 200 {
			return false, nil
		}
		if endpoint == "/v1/models" {
			return ValidateModels(data, p), nil
		}
	}
	return false, nil
}
func Endpoint(e *Evidence) (string, error) {
	bindings := e.NetworkSettings.Ports["8000/tcp"]
	if len(bindings) != 1 || bindings[0].HostIP != "127.0.0.1" {
		return "", fmt.Errorf("runtime port must bind only to host loopback")
	}
	port, err := strconv.Atoi(bindings[0].HostPort)
	if err != nil || port < 1 || port > 65535 {
		return "", fmt.Errorf("invalid runtime host port")
	}
	return "http://127.0.0.1:" + strconv.Itoa(port) + "/v1", nil
}

// Check the adapter alias and its parent; a healthy base alone is insufficient.
func ValidateModels(data []byte, p Plan) bool {
	var list struct {
		Data []struct {
			ID     string `json:"id"`
			Parent string `json:"parent"`
			Root   string `json:"root"`
		} `json:"data"`
	}
	if json.Unmarshal(data, &list) != nil {
		return false
	}
	base, adapter := false, p.Source.Adapter == nil
	for _, m := range list.Data {
		if m.ID == p.BaseModelName() {
			base = true
		}
		if p.Source.Adapter != nil && m.ID == p.ModelName() && m.Parent == p.BaseModelName() && m.Root == "/models/"+p.DeploymentID+"/adapter" {
			adapter = true
		}
	}
	return base && adapter
}
