package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/apispec"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/tooling"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"strconv"
)

func httpResult(j *store.ToolJob, out *platformv1.ToolExecutionResult, err error) ([]byte, string) {
	unknown := func() ([]byte, string) {
		return tooling.JSON(map[string]any{"status": 0, "data": nil, "completion": "unknown"}), "TOOL_EFFECT_UNKNOWN"
	}
	fail := func(code string) ([]byte, string) {
		if j.Snapshot.Version.Binding.Effect == "write" {
			return unknown()
		}
		return nil, code
	}
	if err != nil {
		if status.Code(err) == codes.DeadlineExceeded {
			return fail("TOOL_TIMEOUT")
		}
		return fail("TOOL_UPSTREAM_UNAVAILABLE")
	}
	if out == nil || out.ToolCallId != j.ID || out.ToolVersionId != j.Snapshot.Version.ToolVersionID || out.LeaseToken != j.LeaseToken || !rpc.ValidID(out.RuntimeInstanceId) {
		return fail("TOOL_RESPONSE_INVALID")
	}
	var body struct {
		Status            int             `json:"status"`
		Data              json.RawMessage `json:"data"`
		Completion        string          `json:"completion"`
		ExternalReference string          `json:"external_reference,omitempty"`
	}
	if out.ErrorCode != "" {
		switch out.ErrorCode {
		case "TOOL_EFFECT_UNKNOWN":
			return unknown()
		case "TOOL_ASYNC_UNSUPPORTED":
			if tooling.NullableJSON(out.ResultJson, &body, 4096) != nil || body.Status != 202 || body.Completion != "accepted_only" || string(body.Data) != "null" || !tooling.ValidLabel(body.ExternalReference, 2048, false) {
				return fail("TOOL_RESPONSE_INVALID")
			}
			return tooling.JSON(map[string]any{"status": 202, "data": nil, "completion": "accepted_only", "external_reference": body.ExternalReference, "runtime_instance_id": out.RuntimeInstanceId}), "TOOL_ASYNC_UNSUPPORTED"
		case "TOOL_HTTP_ERROR":
			if tooling.NullableJSON(out.ResultJson, &body, 4096) != nil || body.Status < 300 || body.Status > 599 {
				return fail("TOOL_RESPONSE_INVALID")
			}
			return tooling.JSON(map[string]any{"status": body.Status, "data": nil, "completion": "failed", "runtime_instance_id": out.RuntimeInstanceId}), out.ErrorCode
		case "TOOL_TIMEOUT", "TOOL_TARGET_BLOCKED", "TOOL_RESPONSE_INVALID", "TOOL_RESPONSE_TOO_LARGE", "TOOL_UPSTREAM_UNAVAILABLE", "CREDENTIAL_NOT_CONFIGURED", "CREDENTIAL_REVISION_UNAVAILABLE", "TOOL_INPUT_INVALID":
			return nil, out.ErrorCode
		default:
			return fail("TOOL_RESPONSE_INVALID")
		}
	}
	if tooling.NullableJSON(out.ResultJson, &body, tooling.MaxOutput+4096) != nil || body.Completion != "completed" || body.ExternalReference != "" || len(body.Data) == 0 {
		return fail("TOOL_RESPONSE_INVALID")
	}
	http := j.Snapshot.Version.Binding.HTTP
	if http == nil {
		return fail("TOOL_RESPONSE_INVALID")
	}
	schema, ok := http.Responses[strconv.Itoa(body.Status)]
	if !ok {
		return fail("TOOL_RESPONSE_INVALID")
	}
	if body.Status == 204 {
		if string(body.Data) != "null" {
			return fail("TOOL_RESPONSE_INVALID")
		}
	} else {
		value, e := apispec.JSON(body.Data, tooling.MaxOutput)
		if e != nil || apispec.Validate(schema, value) != nil {
			return fail("TOOL_RESPONSE_INVALID")
		}
	}
	return tooling.JSON(map[string]any{"status": body.Status, "data": body.Data, "completion": "completed", "runtime_instance_id": out.RuntimeInstanceId}), ""
}
