package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/tooling"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"log/slog"
	"sync"
	"time"
)

type AgentJobs interface {
	ClaimAgent(context.Context) (*store.AgentJob, error)
	HeartbeatAgent(context.Context, *store.AgentJob) error
	FinishAgent(context.Context, *store.AgentJob, []byte, string, string) error
}
type AgentWorker struct {
	DB      AgentJobs
	Runtime platformv1.RuntimeServiceClient
}

func (w *AgentWorker) Run(ctx context.Context) {
	var group sync.WaitGroup
	for range 2 {
		group.Go(func() {
			for ctx.Err() == nil {
				dbctx, cancel := context.WithTimeout(ctx, 5*time.Second)
				job, err := w.DB.ClaimAgent(dbctx)
				cancel()
				if err == nil && job != nil {
					w.execute(ctx, job)
					continue
				}
				if err != nil && ctx.Err() == nil {
					slog.Warn("agent_claim_unavailable")
				}
				timer := time.NewTimer(200 * time.Millisecond)
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
func (w *AgentWorker) execute(parent context.Context, j *store.AgentJob) {
	ctx, cancel := context.WithDeadline(parent, j.Deadline)
	defer cancel()
	beatDone := make(chan struct{})
	stopBeat := make(chan struct{})
	go func() {
		defer close(beatDone)
		ticker := time.NewTicker(10 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-stopBeat:
				return
			case <-ctx.Done():
				return
			case <-ticker.C:
				dbctx, stop := context.WithTimeout(ctx, 3*time.Second)
				err := w.DB.HeartbeatAgent(dbctx, j)
				stop()
				if err != nil {
					cancel()
					return
				}
			}
		}
	}()
	in := &platformv1.AgentExecution{AgentRunId: j.ID, AgentVersionId: j.Snapshot.Version.AgentVersionID, LeaseToken: j.LeaseToken, SnapshotJson: tooling.JSON(j.Snapshot), InputJson: j.Input}
	out, err := w.Runtime.ExecuteAgent(rpc.Outgoing(ctx, j.RequestID), in, grpc.WaitForReady(false), grpc.MaxCallRecvMsgSize(3<<20), grpc.MaxCallSendMsgSize(3<<20))
	close(stopBeat)
	<-beatDone
	if parent.Err() != nil {
		return
	} // A successor fences and interrupts; never replays.
	code, stage := "", ""
	var result []byte
	switch {
	case err != nil:
		code, stage = "AGENT_EXECUTION_INTERRUPTED", "execute"
		if status.Code(err) == codes.DeadlineExceeded || time.Now().After(j.Deadline) {
			code = "AGENT_TIMEOUT"
		}
	case out == nil || out.AgentRunId != j.ID || out.AgentVersionId != in.AgentVersionId || out.LeaseToken != j.LeaseToken || !rpc.ValidID(out.RuntimeInstanceId):
		code, stage = "AGENT_RESULT_INVALID", "execute"
	case out.ErrorCode != "":
		code, stage = out.ErrorCode, out.ErrorStage
		if (!agenting.ErrorCode(code) && !tooling.KnowledgeError(code)) || !agenting.Stage(stage) {
			code, stage = "AGENT_RESULT_INVALID", "execute"
		}
	default:
		result = out.ResultJson
	}
	// Retrying a durable report does not repeat the graph or the model request.
	for attempt := 0; attempt < 3; attempt++ {
		dbctx, stop := context.WithTimeout(parent, 3*time.Second)
		err = w.DB.FinishAgent(dbctx, j, result, code, stage)
		stop()
		if err == nil || tooling.Code(err) == "AGENT_EXECUTION_FENCED" {
			break
		}
		select {
		case <-parent.Done():
			return
		case <-time.After(200 * time.Millisecond):
		}
	}
	slog.Info("agent_run_completed", "agent_run_id", j.ID, "request_id", j.RequestID, "error_code", code, "persisted", err == nil)
}
