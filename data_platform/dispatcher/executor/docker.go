package executor

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"os/exec"
	"regexp"
	"strings"
	"time"

	"data-platform/internal/execution"
	"github.com/google/uuid"
)

type CommandFunc func(context.Context, ...string) ([]byte, error)

func CLI(ctx context.Context, args ...string) ([]byte, error) {
	return exec.CommandContext(ctx, "docker", args...).Output()
}

type Docker struct {
	Network string
	Call    CommandFunc
}

func New(network string) *Docker   { return &Docker{Network: network, Call: CLI} }
func Name(id, token string) string { return "data-op-" + id + "-" + token }

var imageID = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

func canonical(s string) bool {
	u, e := uuid.Parse(s)
	return e == nil && u != uuid.Nil && u.String() == s
}

func (d *Docker) Run(ctx context.Context, c execution.Command, p execution.Profile) error {
	reject := func(s string) error { return &execution.RejectedError{Reason: s} }
	if !canonical(c.OperationID) || !canonical(c.ResourceID) || !canonical(c.DispatchToken) {
		return reject("invalid execution identity")
	}
	if !execution.Supported(c.ExecutionType, c.ResourceType) || c.Resources.NodeName != "" || c.Resources.GPUCount != 0 || len(c.Resources.GPUIds) != 0 {
		return reject("only supported local CPU data jobs are allowed")
	}
	raw, err := d.Call(ctx, "image", "inspect", p.Image)
	if err != nil {
		return reject("CPU Job image is not available locally; build the corresponding job target first")
	}
	var images []struct {
		ID     string `json:"Id"`
		Config struct{ Labels map[string]string }
	}
	if json.Unmarshal(raw, &images) != nil || len(images) != 1 || !imageID.MatchString(images[0].ID) || images[0].Config.Labels["data-platform.execution-gate"] != "1" {
		return reject("CPU Job image lacks the data execution gate")
	}
	memory, tmpfs := "512m", "/tmp:rw,noexec,nosuid,size=67108864"
	if c.ExecutionType == "refinement.parse" {
		memory = "1g"
		tmpfs = "/tmp:rw,noexec,nosuid,size=134217728"
	}
	if c.ExecutionType == "collection.browser" {
		memory = "2g"
		tmpfs = "/tmp:rw,noexec,nosuid,size=536870912"
	}
	args := []string{"run", "--detach", "--pull=never", "--name", Name(c.OperationID, c.DispatchToken), "--network", d.Network, "--restart=no", "--read-only", "--tmpfs", tmpfs, "--cap-drop=ALL", "--security-opt", "no-new-privileges", "--cpus=1", "--memory=" + memory, "--pids-limit=128", "--log-opt", "max-size=10m", "--log-opt", "max-file=2"}
	if c.ExecutionType == "collection.browser" {
		args = append(args, "--init", "--shm-size=256m")
	}
	labels := [][2]string{{"owner", "dispatcher"}, {"operation_id", c.OperationID}, {"resource_id", c.ResourceID}, {"execution_type", c.ExecutionType}, {"dispatch_token", c.DispatchToken}}
	for _, v := range labels {
		args = append(args, "--label", "data-platform."+v[0]+"="+v[1])
	}
	// Values are separate argv entries, never shell code. Credentials are passed
	// by environment name; CLI output and error messages never print argv/env.
	for _, name := range p.ForwardEnv {
		if os.Getenv(name) == "" {
			return reject("required Job setting is missing: " + name)
		}
		args = append(args, "--env", name)
	}
	for _, v := range [][2]string{{"OPERATION_ID", c.OperationID}, {"RESOURCE_ID", c.ResourceID}, {"EXECUTION_TYPE", c.ExecutionType}, {"DISPATCH_TOKEN", c.DispatchToken}, {"PROCESSOR_IMAGE_ID", images[0].ID}} {
		args = append(args, "--env", v[0]+"="+v[1])
	}
	args = append(args, images[0].ID)
	args = append(args, p.Command...)
	launchStarted := time.Now()
	_, err = d.Call(ctx, args...)
	log.Printf("data_job_timing_v1 operation_id=%s stage=container_launch elapsed_ms=%d confirmed=%t", c.OperationID, time.Since(launchStarted).Milliseconds(), err == nil)
	if err != nil {
		return fmt.Errorf("Docker launch result unconfirmed")
	}
	return nil
}

type Runtime struct {
	Name   string
	Config struct{ Labels map[string]string }
	State  struct {
		Status  string
		Running bool
	}
}

func (d *Docker) Inspect(ctx context.Context, id, token, resource string) (*Runtime, error) {
	if !canonical(id) || !canonical(token) || !canonical(resource) {
		return nil, fmt.Errorf("invalid runtime identity")
	}
	raw, err := d.Call(ctx, "container", "inspect", Name(id, token))
	if err != nil {
		return nil, fmt.Errorf("runtime observation unavailable")
	}
	var a []Runtime
	if json.Unmarshal(raw, &a) != nil || len(a) != 1 {
		return nil, fmt.Errorf("invalid runtime observation")
	}
	r := a[0]
	l := r.Config.Labels
	if strings.TrimPrefix(r.Name, "/") != Name(id, token) || l["data-platform.owner"] != "dispatcher" || l["data-platform.operation_id"] != id || l["data-platform.dispatch_token"] != token || l["data-platform.resource_id"] != resource || (l["data-platform.execution_type"] != "collection.crawl" && l["data-platform.execution_type"] != "collection.browser" && l["data-platform.execution_type"] != "refinement.parse") {
		return nil, fmt.Errorf("runtime identity mismatch")
	}
	return &r, nil
}
