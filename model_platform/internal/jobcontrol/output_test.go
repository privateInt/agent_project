package jobcontrol

import (
	"context"
	"encoding/json"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"model-platform/internal/gpuadmission"
	"net/http"
	"strings"
	"testing"
)

func TestOutputRecoveryRequiresStoppedCurrentProducer(t *testing.T) {
	for _, which := range []string{"exited", "removed", "running", "wrong_owner", "wrong_node", "cancelled", "no_execution", "inspect_failure"} {
		t.Run(which, func(t *testing.T) {
			op := testOperation()
			op.Status = "failed"
			op.ExecutionStartedAt = timestamppb.Now()
			c := testInspection(op)
			c.State.Status = "exited"
			c.State.Running = false
			c.Config.Labels["model-platform.execution_node"] = "local"
			switch which {
			case "running":
				c.State.Status = "running"
				c.State.Running = true
			case "wrong_owner":
				c.Config.Labels["model-platform.owner"] = "other"
			case "wrong_node":
				c.Config.Labels["model-platform.execution_node"] = "other"
			case "cancelled":
				op.Status = "cancelled"
			case "no_execution":
				op.ExecutionStartedAt = nil
			}
			runtime := &Runtime{Node: "local", HTTP: &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
				if req.Method != "GET" || !strings.HasSuffix(req.URL.Path, "/json") {
					t.Fatal("output check attempted a runtime mutation", req.Method, req.URL.Path)
				}
				code := 200
				if which == "removed" {
					code = 404
				}
				if which == "inspect_failure" {
					code = 500
				}
				raw, _ := json.Marshal(c)
				return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader(string(raw)))}, nil
			})}}
			e := runtime.OutputStopped(context.Background(), op)
			success := which == "exited" || which == "removed"
			if (e == nil) != success {
				t.Fatal(which, e)
			}
		})
	}
}

func TestCancelledCheckpointStopObservationDoesNotAuthorizePublication(t *testing.T) {
	op := testOperation()
	op.Status = "cancelled"
	op.ExecutionStartedAt = timestamppb.Now()
	c := testInspection(op)
	c.State.Status = "exited"
	c.State.Running = false
	c.Config.Labels["model-platform.execution_node"] = "local"
	runtime := &Runtime{Node: "local", HTTP: &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
		if req.Method != "GET" {
			t.Fatal("checkpoint check mutated Docker")
		}
		raw, _ := json.Marshal(c)
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader(string(raw)))}, nil
	})}}
	if e := runtime.CheckpointStopped(context.Background(), op); e != nil {
		t.Fatal(e)
	}
	if e := runtime.OutputStopped(context.Background(), op); e == nil {
		t.Fatal("cancelled checkpoint enabled completed-output publication")
	}
	c.State.Running = true
	c.State.Status = "running"
	if e := runtime.CheckpointStopped(context.Background(), op); e == nil {
		t.Fatal("running producer accepted")
	}
}

func TestPurgeStopObserverHandlesCancelledUnstartedAndAllGenerations(t *testing.T) {
	op := testOperation()
	op.Status = "cancelled"
	op.ExecutionStartedAt = nil
	runtime := &Runtime{Node: "local", Guard: gpuadmission.Guard{Inventory: func(context.Context) ([]gpuadmission.Container, error) { return nil, nil }}}
	if e := runtime.PurgeStopped(context.Background(), op); e != nil {
		t.Fatal(e)
	}
	item := gpuadmission.Container{}
	item.State.Status = "running"
	item.State.Running = true
	item.Config.Labels = map[string]string{"model-platform.operation_id": op.OperationId, "model-platform.owner": "operation-dispatcher", "model-platform.execution_type": "training.run"}
	runtime.Guard.Inventory = func(context.Context) ([]gpuadmission.Container, error) { return []gpuadmission.Container{item}, nil }
	if e := runtime.PurgeStopped(context.Background(), op); e == nil {
		t.Fatal("active producer passed purge")
	}
	item.State.Running = false
	item.State.Status = "exited"
	if e := runtime.PurgeStopped(context.Background(), op); e != nil {
		t.Fatal(e)
	}
}
