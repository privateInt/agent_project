package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"
	"strings"
	"sync"
)

type Docker interface {
	List(context.Context) ([]string, error)
	Inspect(context.Context, string) (Inspect, error)
	Logs(context.Context, string, io.Writer, io.Writer) error
	Remove(context.Context, string) error
}

type dockerCLI struct{}

// A byte budget is shared by stdout/stderr. Overflow fails the collection;
// it never creates a successful but silently truncated archive.
type byteBudget struct {
	mu        sync.Mutex
	remaining int64
}
type limitedWriter struct {
	out    io.Writer
	budget *byteBudget
}

func (w limitedWriter) Write(p []byte) (int, error) {
	w.budget.mu.Lock()
	defer w.budget.mu.Unlock()
	if int64(len(p)) > w.budget.remaining {
		return 0, fmt.Errorf("output exceeds byte limit")
	}
	n, err := w.out.Write(p)
	w.budget.remaining -= int64(n)
	return n, err
}

func dockerOutput(ctx context.Context, args ...string) ([]byte, error) {
	var out bytes.Buffer
	cmd := exec.CommandContext(ctx, "docker", args...)
	cmd.Stdout = limitedWriter{&out, &byteBudget{remaining: 8 << 20}}
	// Docker diagnostics may include connection details. Do not mix them into
	// inspect JSON or echo them into collector logs.
	cmd.Stderr = io.Discard
	if err := cmd.Run(); err != nil {
		return nil, fmt.Errorf("docker %s failed: %w", args[0], err)
	}
	return out.Bytes(), nil
}

func (dockerCLI) List(ctx context.Context) ([]string, error) {
	b, err := dockerOutput(ctx, "ps", "-aq", "--no-trunc", "--filter", "label=model-platform.owner=operation-dispatcher", "--filter", "status=exited")
	if err != nil {
		return nil, err
	}
	ids := strings.Fields(string(b))
	for _, id := range ids {
		if !containerRE.MatchString(id) {
			return nil, fmt.Errorf("invalid container ID returned by Docker")
		}
	}
	return ids, nil
}

// Field selection prevents Config.Env, command arguments and host mount paths
// from ever entering the collector. Network values are used only for membership.
const inspectFormat = `{"ID":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},"RestartCount":{{json .RestartCount}},"Labels":{{json .Config.Labels}},"Tty":{{json .Config.Tty}},"AutoRemove":{{json .HostConfig.AutoRemove}},"RestartPolicy":{{json .HostConfig.RestartPolicy.Name}},"LogDriver":{{json .HostConfig.LogConfig.Type}},"Networks":{{json .NetworkSettings.Networks}},"State":{"Status":{{json .State.Status}},"Running":{{json .State.Running}},"Paused":{{json .State.Paused}},"Restarting":{{json .State.Restarting}},"OOMKilled":{{json .State.OOMKilled}},"ExitCode":{{json .State.ExitCode}},"StartedAt":{{json .State.StartedAt}},"FinishedAt":{{json .State.FinishedAt}}}}`

func (dockerCLI) Inspect(ctx context.Context, id string) (Inspect, error) {
	var c Inspect
	if !containerRE.MatchString(id) {
		return c, fmt.Errorf("invalid container ID")
	}
	b, err := dockerOutput(ctx, "inspect", "--type", "container", "--format", inspectFormat, id)
	if err != nil {
		return c, err
	}
	if err := json.Unmarshal(b, &c); err != nil {
		return c, fmt.Errorf("invalid Docker inspect response")
	}
	if c.ID != id {
		return c, fmt.Errorf("Docker returned a different container ID")
	}
	return c, nil
}

func (dockerCLI) Logs(ctx context.Context, id string, stdout, stderr io.Writer) error {
	if !containerRE.MatchString(id) {
		return fmt.Errorf("invalid container ID")
	}
	cmd := exec.CommandContext(ctx, "docker", "logs", "--timestamps", id)
	cmd.Stdout, cmd.Stderr = stdout, stderr
	if err := cmd.Run(); err != nil {
		return fmt.Errorf("docker logs failed or exceeded byte limit: %w", err)
	}
	return nil
}

func (dockerCLI) Remove(ctx context.Context, id string) error {
	if !containerRE.MatchString(id) {
		return fmt.Errorf("invalid container ID")
	}
	// No --force, --volumes, name lookup, prune or image deletion.
	_, err := dockerOutput(ctx, "rm", id)
	return err
}
