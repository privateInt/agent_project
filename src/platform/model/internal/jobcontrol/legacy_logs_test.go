package jobcontrol

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"testing"

	commonv1 "model-platform/gen/common/v1"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/gpuadmission"
)

func TestLegacyLocalLogsOnNamedController(t *testing.T) {
	tests := []struct {
		name string
		edit func(*inspection, *operationv1.Operation)
		wantError bool
	}{
		{name: "succeeded missing node"},
		{name: "failed missing node", edit: func(_ *inspection, op *operationv1.Operation) { op.Status = "failed" }},
		{name: "cancelled missing node", edit: func(_ *inspection, op *operationv1.Operation) { op.Status = "cancelled" }},
		{name: "legacy missing requested label", edit: func(c *inspection, _ *operationv1.Operation) { delete(c.Config.Labels, "model-platform.requested_node") }},
		{name: "current named running", edit: func(c *inspection, op *operationv1.Operation) {
			c.Config.Labels["model-platform.execution_node"] = "simsys4090"
			c.State.Status, c.State.Running, op.Status = "running", true, "running"
		}},
		{name: "different named node", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.execution_node"] = "simsys4070" }},
		{name: "explicit empty node", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.execution_node"] = "" }},
		{name: "explicit local placement", wantError: true, edit: func(c *inspection, op *operationv1.Operation) {
			op.Resources = &commonv1.ExecutionResources{NodeName: "simsys4090"}
			c.Config.Labels["model-platform.requested_node"] = "simsys4090"
		}},
		{name: "explicit remote placement", wantError: true, edit: func(c *inspection, op *operationv1.Operation) {
			op.Resources = &commonv1.ExecutionResources{NodeName: "simsys4070"}
			c.Config.Labels["model-platform.requested_node"] = "simsys4070"
		}},
		{name: "requested label mismatch", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.requested_node"] = "simsys4070" }},
		{name: "nonterminal operation", wantError: true, edit: func(_ *inspection, op *operationv1.Operation) { op.Status = "running" }},
		{name: "running legacy container", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.State.Status, c.State.Running = "running", true }},
		{name: "created legacy container", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.State.Status = "created" }},
		{name: "inconsistent running flag", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.State.Running = true }},
		{name: "paused legacy container", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.State.Paused = true }},
		{name: "restarting legacy container", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.State.Restarting = true }},
		{name: "foreign owner", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.owner"] = "other" }},
		{name: "foreign operation", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.operation_id"] = "55555555-5555-4555-8555-555555555555" }},
		{name: "different execution type", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Labels["model-platform.execution_type"] = "deployment.apply" }},
		{name: "old dispatch generation", wantError: true, edit: func(c *inspection, op *operationv1.Operation) {
			token := "55555555-5555-4555-8555-555555555555"
			c.Config.Labels["model-platform.dispatch_token"] = token
			c.Name = "/model-op-" + op.GetOperationId() + "-" + token
		}},
		{name: "name mismatch", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Name = "/other" }},
		{name: "invalid container id", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.ID = "short-id" }},
		{name: "tty", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.Config.Tty = true }},
		{name: "auto remove", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.HostConfig.AutoRemove = true }},
		{name: "automatic restart", wantError: true, edit: func(c *inspection, _ *operationv1.Operation) { c.HostConfig.RestartPolicy.Name = "always" }},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			op := testOperation()
			op.Status = "succeeded"
			c := testInspection(op)
			c.State.Status, c.State.Running = "exited", false
			c.HostConfig.RestartPolicy.Name = "no"
			c.Config.Labels["model-platform.requested_node"] = ""
			if tt.edit != nil { tt.edit(&c, op) }
			logRequests := 0
			runtime := &Runtime{Node: "simsys4090", HTTP: &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
				if req.Method != "GET" { t.Fatal("log lookup attempted mutation", req.Method) }
				if strings.HasSuffix(req.URL.Path, "/json") {
					if req.URL.Path != "/v1.45/containers/model-op-"+op.OperationId+"-"+op.DispatchToken+"/json" { t.Fatal("inspection used wrong dispatch") }
					raw, err := json.Marshal(c)
					if err != nil { t.Fatal(err) }
					return response(200, raw), nil
				}
				if req.URL.Path != "/v1.45/containers/"+c.ID+"/logs" { t.Fatal("logs must use immutable container ID", req.URL.Path) }
				logRequests++
				return response(200, append(frame(1, "saved output\n"), frame(2, "saved warning\n")...)), nil
			})}}
			out, err := runtime.Logs(context.Background(), op, 20, 1024)
			if tt.wantError {
				if err == nil || logRequests != 0 { t.Fatal("unverified logs returned", out, err, logRequests) }
				return
			}
			if err != nil || logRequests != 1 || out.GetSource() != "docker" || out.GetAvailability() != "available" ||
				out.GetStdout() != "saved output\n" || out.GetStderr() != "saved warning\n" ||
				out.GetOperationId() != op.OperationId || out.GetTrainingRunId() != op.ResourceId { t.Fatal(out, err, logRequests) }
		})
	}
}

func TestLegacyLogCompatibilityDoesNotAuthorizeCancellation(t *testing.T) {
	for _, state := range []string{"running", "exited"} {
		t.Run(state, func(t *testing.T) {
			op := testOperation()
			c := testInspection(op)
			c.State.Status, c.State.Running = state, state == "running"
			mutations := 0
			runtime := &Runtime{Node: "simsys4090", Guard: gpuadmission.Guard{Dir: t.TempDir(), Inventory: func(context.Context) ([]gpuadmission.Container, error) {
				item := gpuadmission.Container{ID: c.ID, Name: c.Name}
				item.Config.Labels = c.Config.Labels
				item.State.Status, item.State.Running = c.State.Status, c.State.Running
				return []gpuadmission.Container{item}, nil
			}}, HTTP: &http.Client{Transport: roundTrip(func(req *http.Request) (*http.Response, error) {
				if req.Method != "GET" { mutations++; return response(204, nil), nil }
				raw, err := json.Marshal(c)
				if err != nil { t.Fatal(err) }
				return response(200, raw), nil
			})}}
			if err := runtime.Cancel(context.Background(), op); err == nil || mutations != 0 { t.Fatal("legacy log exception authorized cancellation", err, mutations) }
		})
	}
}
