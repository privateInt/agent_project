package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"log/slog"
	"sync"
	"time"
)

type JobRepository interface {
	ClaimTool(context.Context) (*store.ToolJob, error)
	FinishTool(context.Context, *store.ToolJob, []byte, string) error
}
type ToolWorker struct {
	DB      JobRepository
	Runtime platformv1.RuntimeServiceClient
}

func (w *ToolWorker) Run(ctx context.Context) {
	var group sync.WaitGroup
	for range 2 {
		group.Go(func() {
			for ctx.Err() == nil {
				dbctx, cancel := context.WithTimeout(ctx, 5*time.Second)
				job, err := w.DB.ClaimTool(dbctx)
				cancel()
				if err == nil && job != nil {
					w.execute(ctx, job)
					continue
				}
				if err != nil && ctx.Err() == nil {
					slog.Warn("tool_claim_unavailable")
				}
				timer := time.NewTimer(100 * time.Millisecond)
				select {
				case <-ctx.Done():
					timer.Stop()
					return
				case <-timer.C:
				}
			}
		})
	}
	group.Wait()
}
func (w *ToolWorker) execute(parent context.Context, j *store.ToolJob) {
	ctx, cancel := context.WithDeadline(parent, j.Deadline)
	defer cancel()
	in := &platformv1.ToolExecution{ToolCallId: j.ID, ToolVersionId: j.Snapshot.Version.ToolVersionID, Kind: j.Snapshot.Version.Kind, BindingJson: tooling.JSON(j.Snapshot.Version.Binding), ArgumentsJson: j.Arguments, LeaseToken: j.LeaseToken, InvocationKey: j.InvocationKey}
	if j.Snapshot.Connection != nil {
		in.ConnectionJson = tooling.JSON(j.Snapshot.Connection)
	}
	if j.Snapshot.Version.Kind == "knowledge_search_v1" {
		in.ConnectionJson = tooling.JSON(map[string]any{"knowledge": j.Snapshot.Connection, "model": j.Snapshot.ModelConnection})
	}
	out, err := w.Runtime.ExecuteTool(toolTraceContext(ctx, j), in, grpc.WaitForReady(false))
	// During process shutdown the running lease is left intact. Its expiry is
	// recorded as interrupted. A successor does not execute this job again.
	if parent.Err() != nil {
		return
	}
	code := ""
	var result []byte
	if j.Snapshot.Version.Kind == "knowledge_search_v1" {
		result, code = knowledgeResult(j, out, err)
	} else if j.Snapshot.Version.Kind == "openapi_http_v1" {
		result, code = httpResult(j, out, err)
	} else {
		switch {
		case err != nil:
			code = "TOOL_UPSTREAM_UNAVAILABLE"
			if status.Code(err) == codes.DeadlineExceeded || ctx.Err() != nil {
				code = "TOOL_TIMEOUT"
			}
		case out.ToolCallId != j.ID || out.ToolVersionId != in.ToolVersionId || out.LeaseToken != j.LeaseToken || !rpc.ValidID(out.RuntimeInstanceId):
			code = "TOOL_RESPONSE_INVALID"
		case out.ErrorCode != "":
			switch out.ErrorCode {
			case "TOOL_TIMEOUT", "TOOL_DIAGNOSTIC_FAILURE", "CREDENTIAL_NOT_CONFIGURED", "CREDENTIAL_REVISION_UNAVAILABLE", "TOOL_INPUT_INVALID":
				code = out.ErrorCode
			default:
				code = "TOOL_RESPONSE_INVALID"
			}
		case tooling.ValidateOutput(j.Snapshot.Version, out.ResultJson) != nil:
			code = "TOOL_RESPONSE_INVALID"
		default:
			result = tooling.JSON(map[string]any{"data": json.RawMessage(out.ResultJson), "completion": "completed", "runtime_instance_id": out.RuntimeInstanceId})
		}
	}
	// A lost result report may be retried, but ExecuteTool itself is never retried.
	for attempts := 0; attempts < 3; attempts++ {
		dbctx, stop := context.WithTimeout(parent, 3*time.Second)
		err = w.DB.FinishTool(dbctx, j, result, code)
		stop()
		if err == nil || tooling.Code(err) == "TOOL_EXECUTION_FENCED" {
			break
		}
		timer := time.NewTimer(200 * time.Millisecond)
		select {
		case <-parent.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
	slog.Info("tool_call_completed", "tool_call_id", j.ID, "request_id", j.RequestID, "error_code", code, "persisted", err == nil)
}

// Only the durable parent binding can establish tracing ancestry. Never use
// caller-provided trace headers, arguments, or the idempotency key.
func toolTraceContext(ctx context.Context, j *store.ToolJob) context.Context {
	ctx = rpc.Outgoing(ctx, j.RequestID)
	if j.ParentAgentRunID != nil && rpc.ValidID(*j.ParentAgentRunID) && j.ParentStepID != nil && *j.ParentStepID == "search" {
		ctx = metadata.AppendToOutgoingContext(ctx, "x-agent-parent-run-id", *j.ParentAgentRunID, "x-agent-parent-step-id", *j.ParentStepID)
	}
	return ctx
}
