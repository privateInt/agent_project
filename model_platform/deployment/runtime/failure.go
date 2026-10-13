package runtime

import (
	"context"
	"strings"
	"time"
)

// Diagnostic text never echoes model-generated or arbitrary runtime log lines.
func ClassifyFailure(hostOOM bool, logs string) (string, string) {
	if hostOOM {
		return "RUNTIME_HOST_OOM", "runtime was killed by the host memory limit"
	}
	lower := strings.ToLower(logs)
	for _, marker := range []string{"cuda out of memory", "cuda error: out of memory", "torch.outofmemoryerror", "no available memory for the cache blocks", "free memory on device", "not enough memory to hold"} {
		if strings.Contains(lower, marker) {
			return "GPU_OUT_OF_MEMORY", "model could not load within available GPU memory; lower GPU memory utilization or stop another deployment before a new request"
		}
	}
	return "RUNTIME_NOT_RUNNING", "vLLM exited; automatic restart is disabled; inspect runtime logs"
}
func (d Docker) Failure(ctx context.Context, e *Evidence) (string, string) {
	if e == nil || !containerID.MatchString(e.ID) {
		return ClassifyFailure(false, "")
	}
	// Bounded tail; Docker logs are not included in the public error response.
	logs, _ := run(ctx, 5*time.Second, "logs", "--tail", "80", e.ID)
	return ClassifyFailure(e.State.OOMKilled, string(logs))
}
