package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	operationv1 "data-platform/gen/operation/v1"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type cleanupOps struct {
	operationv1.OperationServiceClient
	op     *operationv1.Operation
	err    error
	reads  int
	onRead func(int)
}

func (o *cleanupOps) GetOperation(context.Context, *operationv1.GetOperationRequest, ...grpc.CallOption) (*operationv1.Operation, error) {
	o.reads++
	if o.onRead != nil {
		o.onRead(o.reads)
	}
	return o.op, o.err
}

type cleanupFixture struct {
	c         *JobCleanup
	ops       *cleanupOps
	r         cleanupRuntime
	calls     [][]string
	inspects  int
	onInspect func(int)
	removeErr error
}

func newCleanupFixture() *cleanupFixture {
	now := time.Date(2026, 9, 16, 12, 0, 0, 0, time.UTC)
	id, token, resource := "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"
	f := &cleanupFixture{ops: &cleanupOps{op: &operationv1.Operation{
		OperationId: id, DispatchToken: token, ResourceId: resource, ResourceType: "refinement_run",
		ExecutionType: "refinement.parse", DispatchProtocol: 1, Status: "succeeded",
		ExecutionInstanceId: "44444444-4444-4444-8444-444444444444",
		ExecutionStartedAt:  timestamppb.New(now.Add(-3 * time.Hour)), CompletedAt: timestamppb.New(now.Add(-2 * time.Hour)),
	}}}
	f.r.ID, f.r.Name, f.r.Image = strings.Repeat("a", 64), "/data-op-"+id+"-"+token, "sha256:"+strings.Repeat("b", 64)
	f.r.Labels = map[string]string{"data-platform.owner": "dispatcher", "data-platform.execution-gate": "1", "data-platform.operation_id": id,
		"data-platform.dispatch_token": token, "data-platform.resource_id": resource, "data-platform.execution_type": "refinement.parse"}
	f.r.Networks = map[string]json.RawMessage{"ops-infra": json.RawMessage(`{}`)}
	f.r.RestartPolicy, f.r.State.Status = "no", "exited"
	f.r.State.StartedAt, f.r.State.FinishedAt = now.Add(-3*time.Hour), now.Add(-2*time.Hour)
	f.c = &JobCleanup{Operations: f.ops, Mode: "apply", Network: "ops-infra", SuccessKeep: time.Hour, FailureKeep: 7 * 24 * time.Hour, Interval: 5 * time.Minute, Now: func() time.Time { return now }}
	f.c.Call = func(_ context.Context, args ...string) ([]byte, error) {
		f.calls = append(f.calls, append([]string(nil), args...))
		switch args[0] {
		case "container":
			f.inspects++
			if f.onInspect != nil {
				f.onInspect(f.inspects)
			}
			return json.Marshal(f.r)
		case "rm":
			return nil, f.removeErr
		case "ps":
			return []byte(f.r.ID + "\n"), nil
		}
		return nil, errors.New("unexpected Docker command")
	}
	return f
}

func (f *cleanupFixture) removed() bool {
	for _, c := range f.calls {
		if c[0] == "rm" {
			return true
		}
	}
	return false
}

func TestJobCleanupRemovesOnlyAfterDoubleObservation(t *testing.T) {
	f := newCleanupFixture()
	action, err := f.c.process(context.Background(), f.r.ID)
	if err != nil || action != "removed" || f.inspects != 2 || f.ops.reads != 2 {
		t.Fatal(action, err, f.calls)
	}
	if !reflect.DeepEqual(f.calls[len(f.calls)-1], []string{"rm", f.r.ID}) {
		t.Fatal("force/volumes or wrong ID", f.calls)
	}
	if strings.Contains(cleanupInspectFormat, ".Config.Env") {
		t.Fatal("credentials included in inspection")
	}
}

func TestJobCleanupRetentionUsesLaterCompletionAndFailurePolicy(t *testing.T) {
	for _, name := range []string{"recent runtime", "recent operation", "failed", "cancelled", "exit mismatch", "oom"} {
		t.Run(name, func(t *testing.T) {
			f := newCleanupFixture()
			switch name {
			case "recent runtime":
				f.r.State.FinishedAt = f.c.now().Add(-time.Minute)
			case "recent operation":
				f.ops.op.CompletedAt = timestamppb.New(f.c.now().Add(-time.Minute))
			case "failed":
				f.ops.op.Status = "failed"
			case "cancelled":
				f.ops.op.Status = "cancelled"
			case "exit mismatch":
				f.r.State.ExitCode = 1
			case "oom":
				f.r.State.OOMKilled = true
			}
			action, err := f.c.process(context.Background(), f.r.ID)
			if action != "retained" || err != nil || f.removed() {
				t.Fatal(action, err)
			}
		})
	}
	f := newCleanupFixture()
	f.ops.op.Status = "failed"
	f.r.State.StartedAt = f.c.now().Add(-9 * 24 * time.Hour)
	f.r.State.FinishedAt = f.c.now().Add(-8 * 24 * time.Hour)
	f.ops.op.ExecutionStartedAt = timestamppb.New(f.r.State.StartedAt)
	f.ops.op.CompletedAt = timestamppb.New(f.r.State.FinishedAt)
	if action, err := f.c.process(context.Background(), f.r.ID); action != "removed" || err != nil {
		t.Fatal(action, err)
	}
}

func TestJobCleanupRejectsUnconfirmedOrForeignJobs(t *testing.T) {
	cases := map[string]func(*cleanupFixture){
		"running":                 func(f *cleanupFixture) { f.r.State.Status = "running"; f.r.State.Running = true },
		"paused":                  func(f *cleanupFixture) { f.r.State.Paused = true },
		"restarting":              func(f *cleanupFixture) { f.r.State.Restarting = true },
		"restart policy":          func(f *cleanupFixture) { f.r.RestartPolicy = "always" },
		"restarted":               func(f *cleanupFixture) { f.r.RestartCount = 1 },
		"auto remove":             func(f *cleanupFixture) { f.r.AutoRemove = true },
		"model serving":           func(f *cleanupFixture) { f.r.Name = "/model-serving-other" },
		"foreign owner":           func(f *cleanupFixture) { f.r.Labels["data-platform.owner"] = "other" },
		"old token":               func(f *cleanupFixture) { f.ops.op.DispatchToken = "55555555-5555-4555-8555-555555555555" },
		"wrong resource":          func(f *cleanupFixture) { f.ops.op.ResourceId = "55555555-5555-4555-8555-555555555555" },
		"legacy":                  func(f *cleanupFixture) { f.ops.op.DispatchProtocol = 0 },
		"unstarted":               func(f *cleanupFixture) { f.ops.op.ExecutionStartedAt = nil },
		"no completion":           func(f *cleanupFixture) { f.ops.op.CompletedAt = nil },
		"operation still running": func(f *cleanupFixture) { f.ops.op.Status = "running" },
		"lookup failure":          func(f *cleanupFixture) { f.ops.err = errors.New("unavailable") },
		"wrong network":           func(f *cleanupFixture) { f.r.Networks = nil },
		"unsupported type":        func(f *cleanupFixture) { f.ops.op.ExecutionType = "training.run" },
		"future exit":             func(f *cleanupFixture) { f.r.State.FinishedAt = f.c.now().Add(time.Hour) },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			f := newCleanupFixture()
			change(f)
			if action, err := f.c.process(context.Background(), f.r.ID); action != "retained" || err == nil || f.removed() {
				t.Fatal(action, err)
			}
		})
	}
}

func TestJobCleanupStateChangeBeforeRemoveIsRetained(t *testing.T) {
	for _, kind := range []string{"runtime", "operation", "API unavailable"} {
		t.Run(kind, func(t *testing.T) {
			f := newCleanupFixture()
			f.onInspect = func(n int) {
				if n == 2 && kind == "runtime" {
					f.r.State.Running = true
				}
			}
			f.ops.onRead = func(n int) {
				if n == 2 {
					if kind == "operation" {
						f.ops.op.Status = "running"
					}
					if kind == "API unavailable" {
						f.ops.err = errors.New("down")
					}
				}
			}
			if action, err := f.c.process(context.Background(), f.r.ID); action != "retained" || err == nil || f.removed() {
				t.Fatal(action, err)
			}
		})
	}
}

func TestJobCleanupModesAndRemoveError(t *testing.T) {
	for _, mode := range []string{"off", "dry-run", "unknown"} {
		t.Run(mode, func(t *testing.T) {
			f := newCleanupFixture()
			f.c.Mode = mode
			action, _ := f.c.process(context.Background(), f.r.ID)
			if f.removed() || mode == "off" && len(f.calls) > 0 || mode == "dry-run" && action != "would_remove" {
				t.Fatal(action, f.calls)
			}
		})
	}
	f := newCleanupFixture()
	f.removeErr = errors.New("restarted concurrently")
	if action, err := f.c.process(context.Background(), f.r.ID); action != "retained" || err == nil {
		t.Fatal(action, err)
	}
}

func TestJobCleanupListsOnlyExitedOwnedJobs(t *testing.T) {
	f := newCleanupFixture()
	f.c.Tick(context.Background())
	want := []string{"ps", "-aq", "--no-trunc", "--filter", "label=data-platform.owner=dispatcher", "--filter", "status=exited"}
	if !reflect.DeepEqual(f.calls[0], want) || !f.removed() {
		t.Fatal(f.calls)
	}
}

func TestJobCleanupBatchRotationIncludesRetainedCandidates(t *testing.T) {
	f := newCleanupFixture()
	seen := map[string]bool{}
	ids := []string{}
	for i := 1; i <= 25; i++ {
		ids = append(ids, fmt.Sprintf("%064x", i))
	}
	f.c.Call = func(_ context.Context, args ...string) ([]byte, error) {
		if args[0] == "ps" {
			return []byte(strings.Join(ids, "\n")), nil
		}
		if args[0] == "container" {
			seen[args[len(args)-1]] = true
			return nil, errors.New("keep fixture")
		}
		t.Fatal("unexpected mutation", args)
		return nil, nil
	}
	f.c.Tick(context.Background())
	if len(seen) != 20 {
		t.Fatal("batch bound", len(seen))
	}
	f.c.Tick(context.Background())
	if len(seen) != 25 {
		t.Fatal("retained candidates starved other IDs", len(seen))
	}
}
