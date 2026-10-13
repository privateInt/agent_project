package app

// Opt-in Docker acceptance. Only this test's UUID CPU fixture is touched;
// Operation responses are controlled, with no DB writes or data requests.
import (
	"context"
	"encoding/json"
	"os"
	"strings"
	"testing"
	"time"

	"data-platform/dispatcher/executor"
	"github.com/google/uuid"
	"google.golang.org/protobuf/types/known/timestamppb"
)

func TestLiveDataJobCleanup(t *testing.T) {
	image, network := os.Getenv("DATA_CLEANUP_TEST_IMAGE"), os.Getenv("DATA_CLEANUP_TEST_NETWORK")
	if image == "" {
		t.Skip("set DATA_CLEANUP_TEST_IMAGE and DATA_CLEANUP_TEST_NETWORK for an isolated Docker fixture")
	}
	if !strings.HasPrefix(image, "sha256:") || !cleanupContainerID.MatchString(strings.TrimPrefix(image, "sha256:")) || network == "" {
		t.Fatal("an immutable local image and network are required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Minute)
	defer cancel()
	f := newCleanupFixture()
	op := f.ops.op
	op.OperationId, op.DispatchToken, op.ResourceId, op.ExecutionInstanceId = uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	name := executor.Name(op.OperationId, op.DispatchToken)
	// Register recovery before launch, including a lost docker run reply.
	t.Cleanup(func() {
		q, done := context.WithTimeout(context.Background(), 15*time.Second)
		defer done()
		raw, err := executor.CLI(q, "ps", "-aq", "--no-trunc", "--filter", "label=data-platform.cleanup-test="+op.OperationId)
		if err != nil {
			t.Error("fixture cleanup listing failed")
			return
		}
		for _, id := range strings.Fields(string(raw)) {
			data, err := executor.CLI(q, "container", "inspect", "--format", cleanupInspectFormat, id)
			var value cleanupRuntime
			if err != nil || json.Unmarshal(data, &value) != nil || value.Name != "/"+name || value.Labels["data-platform.cleanup-test"] != op.OperationId {
				t.Error("fixture cleanup identity mismatch")
				continue
			}
			// Only the independently named, owned test fixture may be forced on failure.
			if _, err := executor.CLI(q, "rm", "--force", id); err != nil {
				t.Error("fixture cleanup failed")
			}
		}
	})
	raw, err := executor.CLI(ctx, "run", "-d", "--pull=never", "--name", name, "--network", network, "--restart=no",
		"--label", "data-platform.owner=dispatcher", "--label", "data-platform.execution-gate=1",
		"--label", "data-platform.operation_id="+op.OperationId, "--label", "data-platform.dispatch_token="+op.DispatchToken,
		"--label", "data-platform.resource_id="+op.ResourceId, "--label", "data-platform.execution_type=refinement.parse",
		"--label", "data-platform.cleanup-test="+op.OperationId, image, "/bin/sh", "-c", "exit 0")
	if err != nil {
		t.Fatal("CPU fixture launch failed")
	}
	id := strings.TrimSpace(string(raw))
	var runtime cleanupRuntime
	for {
		data, err := executor.CLI(ctx, "container", "inspect", "--format", cleanupInspectFormat, id)
		if err != nil || json.Unmarshal(data, &runtime) != nil {
			t.Fatal("fixture inspection failed")
		}
		if runtime.State.Status == "exited" {
			break
		}
		select {
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-time.After(100 * time.Millisecond):
		}
	}
	op.ExecutionStartedAt, op.CompletedAt = timestamppb.New(runtime.State.StartedAt), timestamppb.New(runtime.State.FinishedAt)
	f.c.Call, f.c.Network, f.c.SuccessKeep = executor.CLI, network, time.Second
	f.c.Now = func() time.Time { return runtime.State.FinishedAt.Add(2 * time.Second) }
	f.c.Mode = "dry-run"
	if action, err := f.c.process(ctx, id); err != nil || action != "would_remove" {
		t.Fatal(action, err)
	}
	if _, err := executor.CLI(ctx, "container", "inspect", "--format", "{{.Id}}", id); err != nil {
		t.Fatal("dry-run deleted fixture")
	}
	f.c.Mode = "apply"
	if action, err := f.c.process(ctx, id); err != nil || action != "removed" {
		t.Fatal(action, err)
	}
	if _, err := executor.CLI(ctx, "container", "inspect", "--format", "{{.Id}}", id); err == nil {
		t.Fatal("fixture still exists")
	}
}
