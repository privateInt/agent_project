package app

import (
	"context"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	serving "model-platform/deployment/runtime"
	commonv1 "model-platform/gen/common/v1"
	deploymentv1 "model-platform/gen/deployment/v1"
	"os"
	"path/filepath"
	"testing"
)

type oomRuntime struct{ *runtimeDriver }

func (d oomRuntime) Failure(context.Context, *serving.Evidence) (string, string) {
	return serving.ClassifyFailure(false, "CUDA out of memory")
}
func TestServingOOMFailsOnceAndNeverRestarts(t *testing.T) {
	f := newServingFixture(t)
	f.s.runtime.driver = oomRuntime{f.driver}
	f.grant(t)
	f.cycle(t)
	f.driver.mu.Lock()
	f.driver.evidence.State.Running = false
	f.driver.evidence.State.Status = "exited"
	f.driver.mu.Unlock()
	f.cycle(t)
	f.cycle(t)
	if r := f.view(t); r.GetApplyStatus() != "failed" || r.GetRuntimeErrorCode() != "GPU_OUT_OF_MEMORY" {
		t.Fatal(r)
	}
	if f.driver.creates != 1 || f.driver.starts != 1 {
		t.Fatal("OOM was retried", f.driver.creates, f.driver.starts)
	}
}
func TestModelPurgeRequiresStoppedServingAndRemovesOnlyItsCache(t *testing.T) {
	f := newServingFixture(t)
	ctx := context.Background()
	f.grant(t)
	f.driver.setHealthy(true)
	f.cycle(t)
	f.cycle(t)
	target := &deploymentv1.ModelPurgeFence{ModelVersionId: f.sourceID}
	if _, e := f.s.BlockModelPurge(ctx, target); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("running model fenced", e)
	}
	if f.stop(t).Status != "stopped" {
		t.Fatal("stop incomplete")
	}
	root := t.TempDir()
	id := f.request.DeploymentId
	other := uuid.NewString()
	for _, name := range []string{id, other} {
		if e := os.MkdirAll(filepath.Join(root, name), 0700); e != nil {
			t.Fatal(e)
		}
		if e := os.WriteFile(filepath.Join(root, name, "weights"), []byte("model"), 0600); e != nil {
			t.Fatal(e)
		}
	}
	f.s.runtime.fetcher = serving.SnapshotFetcher{Root: root}
	if _, e := f.s.BlockModelPurge(ctx, target); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Stat(filepath.Join(root, id)); !os.IsNotExist(e) {
		t.Fatal("selected cache remains", e)
	}
	if _, e := os.Stat(filepath.Join(root, other, "weights")); e != nil {
		t.Fatal("unrelated cache removed", e)
	}
	if _, e := f.s.CreateServingDeployment(ctx, &deploymentv1.DeployModelRequest{ModelVersionId: f.sourceID, IdempotencyKey: uuid.NewString(), Serving: &deploymentv1.ServingSpec{Engine: "vllm"}, Resources: &commonv1.ExecutionResources{GpuIds: []int32{0}}}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("new deployment passed purge fence", e)
	}
}
