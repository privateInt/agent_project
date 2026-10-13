package runtime

import "testing"

func TestRuntimeMemoryFailuresAreSpecificAndDoNotEchoLogs(t *testing.T) {
	for _, input := range []string{"torch.OutOfMemoryError: CUDA out of memory. secret", "ValueError: Free memory on device cuda:0 is less than desired GPU memory utilization", "No available memory for the cache blocks"} {
		code, msg := ClassifyFailure(false, input)
		if code != "GPU_OUT_OF_MEMORY" || msg == input {
			t.Fatal(code, msg)
		}
	}
	if c, _ := ClassifyFailure(true, "CUDA out of memory"); c != "RUNTIME_HOST_OOM" {
		t.Fatal(c)
	}
	if c, _ := ClassifyFailure(false, "invalid model config"); c != "RUNTIME_NOT_RUNNING" {
		t.Fatal(c)
	}
}
