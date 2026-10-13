package executor

import (
	"context"
	"data-platform/internal/execution"
	"errors"
	"strings"
	"testing"
)

func command() execution.Command {
	return execution.Command{OperationID: "11111111-1111-4111-8111-111111111111", DispatchToken: "22222222-2222-4222-8222-222222222222", ResourceID: "33333333-3333-4333-8333-333333333333", ResourceType: "crawl_run", ExecutionType: "collection.crawl"}
}
func TestCPULaunchPinsGatedImageAndSeparatesArguments(t *testing.T) {
	var calls [][]string
	id := "sha256:" + strings.Repeat("a", 64)
	d := &Docker{Network: "ops-infra", Call: func(_ context.Context, a ...string) ([]byte, error) {
		calls = append(calls, a)
		if a[0] == "image" {
			return []byte(`[{"Id":"` + id + `","Config":{"Labels":{"data-platform.execution-gate":"1"}}}]`), nil
		}
		return []byte("container-id"), nil
	}}
	if e := d.Run(context.Background(), command(), execution.Profile{Image: "crawl:dev"}); e != nil {
		t.Fatal(e)
	}
	if len(calls) != 2 {
		t.Fatal(calls)
	}
	args := strings.Join(calls[1], " ")
	for _, want := range []string{"--read-only", "--restart=no", "--cpus=1", "--memory=512m", id, Name(command().OperationID, command().DispatchToken)} {
		if !strings.Contains(args, want) {
			t.Fatal("missing boundary", want)
		}
	}
	for _, bad := range []string{"--gpus", "docker.sock", "crawl:dev", "--privileged"} {
		if strings.Contains(args, bad) {
			t.Fatal("unsafe creation args", bad)
		}
	}
}
func TestMissingImageIsPrelaunchRejectionButCreateTimeoutIsUnknown(t *testing.T) {
	d := &Docker{Call: func(context.Context, ...string) ([]byte, error) { return nil, errors.New("offline") }}
	var rejected *execution.RejectedError
	if e := d.Run(context.Background(), command(), execution.Profile{}); !errors.As(e, &rejected) {
		t.Fatal("image failure was not a safe rejection", e)
	}
	calls := 0
	d.Call = func(_ context.Context, a ...string) ([]byte, error) {
		calls++
		if calls == 1 {
			return []byte(`[{"Id":"sha256:` + strings.Repeat("a", 64) + `","Config":{"Labels":{"data-platform.execution-gate":"1"}}}]`), nil
		}
		return nil, errors.New("timeout")
	}
	e := d.Run(context.Background(), command(), execution.Profile{})
	if e == nil || errors.As(e, &rejected) {
		t.Fatal("uncertain create classified as safe failure", e)
	}
}
func TestGPUOrRemoteRequestsNeverReachDocker(t *testing.T) {
	called := false
	d := &Docker{Call: func(context.Context, ...string) ([]byte, error) { called = true; return nil, nil }}
	for _, r := range []execution.Resources{{GPUCount: 1}, {GPUIds: []int32{0}}, {NodeName: "other"}} {
		cmd := command()
		cmd.Resources = r
		if d.Run(context.Background(), cmd, execution.Profile{}) == nil || called {
			t.Fatal("unsupported placement reached Docker")
		}
	}
}
func TestRefinementLaunchAndResourcePair(t *testing.T) {
	var args []string
	d := &Docker{Call: func(_ context.Context, a ...string) ([]byte, error) {
		if a[0] == "image" {
			return []byte(`[{"Id":"sha256:` + strings.Repeat("a", 64) + `","Config":{"Labels":{"data-platform.execution-gate":"1"}}}]`), nil
		}
		args = a
		return nil, nil
	}}
	c := command()
	c.ExecutionType = "refinement.parse"
	if d.Run(context.Background(), c, execution.Profile{}) == nil {
		t.Fatal("refinement accepted crawl resource")
	}
	c.ResourceType = "refinement_run"
	if e := d.Run(context.Background(), c, execution.Profile{}); e != nil {
		t.Fatal(e)
	}
	joined := strings.Join(args, " ")
	if !strings.Contains(joined, "--memory=1g") || strings.Contains(joined, "--gpus") {
		t.Fatal("invalid CPU refinement boundary")
	}
}

func TestBrowserUsesSeparateBoundedCPUProfile(t *testing.T) {
	var args []string
	d := &Docker{Call: func(_ context.Context, a ...string) ([]byte, error) {
		if a[0] == "image" {
			return []byte(`[{"Id":"sha256:` + strings.Repeat("a", 64) + `","Config":{"Labels":{"data-platform.execution-gate":"1"}}}]`), nil
		}
		args = a
		return nil, nil
	}}
	c := command()
	c.ExecutionType = "collection.browser"
	if e := d.Run(context.Background(), c, execution.Profile{Image: "browser:dev"}); e != nil {
		t.Fatal(e)
	}
	joined := strings.Join(args, " ")
	for _, s := range []string{"--memory=2g", "--init", "--shm-size=256m", "--read-only", "--cap-drop=ALL", "--cpus=1", "--pids-limit=128"} {
		if !strings.Contains(joined, s) {
			t.Fatal("missing browser limit", s)
		}
	}
	for _, s := range []string{"--gpus", "--privileged", "seccomp=unconfined", "docker.sock"} {
		if strings.Contains(joined, s) {
			t.Fatal("unsafe browser profile", s)
		}
	}
}
