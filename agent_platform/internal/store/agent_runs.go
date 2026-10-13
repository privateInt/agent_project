package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"time"
)

func (s *Store) acceptAgent(ctx context.Context, tx pgx.Tx, in *platformv1.ToolRequest, raw []byte) (*platformv1.ToolResponse, error) {
	var req agenting.RunCreate
	if tooling.StrictJSON(raw, &req) != nil || !rpc.ValidID(req.AgentVersionID) {
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	if req.Mode == "" {
		req.Mode = "run"
	}
	if req.Mode != "run" && req.Mode != "test" {
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	for _, v := range []string{req.CallerContext.System, req.CallerContext.WorkflowID, req.CallerContext.ExecutionID, req.CallerContext.NodeID} {
		if !tooling.ValidLabel(v, 200, false) {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
	}
	fingerprint := tooling.Hash(tooling.JSON(req))
	var oldHash, oldID, oldVersion string
	err := tx.QueryRow(ctx, `SELECT fingerprint,agent_run_id::text,agent_version_id::text FROM agent.agent_runs WHERE principal=$1 AND invocation_key=$2`, tooling.Principal, in.IdempotencyKey).Scan(&oldHash, &oldID, &oldVersion)
	if err == nil {
		if fingerprint != oldHash {
			return nil, tooling.E("KEY_CONFLICT")
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(acceptedAgent(oldID, oldVersion)), Replayed: true}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	var snap agenting.Snapshot
	if err = loadDocument(ctx, tx, "agent_versions", "agent_version_id", req.AgentVersionID, &snap.Version); err != nil {
		return nil, err
	}
	var a agenting.Agent
	if err = loadDocument(ctx, tx, "agents", "agent_id", snap.Version.AgentID, &a); err != nil {
		return nil, err
	}
	if !a.Enabled || (req.Mode == "run" && snap.Version.State != "published") || (req.Mode == "test" && snap.Version.State != "draft") {
		return nil, tooling.E("AGENT_VERSION_UNAVAILABLE")
	}
	snap.Tool, err = s.agentTool(ctx, tx, snap.Version.ToolVersionID)
	if err != nil {
		return nil, err
	}
	if err = agenting.ValidateInput(req.Input, snap.Tool.Version); err != nil {
		return nil, err
	}
	if err = loadDocument(ctx, tx, "connection_versions", "connection_version_id", snap.Version.Model.ConnectionVersionID, &snap.ModelConnection); err != nil {
		return nil, err
	}
	snap.CallerContext = req.CallerContext
	snap.Tool.CallerContext = req.CallerContext
	now := tooling.Now()
	id := rpc.NewID()
	deadline := now.Add(time.Duration(snap.Version.TimeoutMS) * time.Millisecond)
	input := tooling.JSON(req.Input)
	_, err = tx.Exec(ctx, `INSERT INTO agent.agent_runs(agent_run_id,agent_version_id,request_id,principal,invocation_key,fingerprint,input_sha256,input,snapshot,mode,state,created_at,deadline_at,execution_lane) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,'accepted',$11,$12,$13)`, id, req.AgentVersionID, rpc.RequestID(ctx), tooling.Principal, in.IdempotencyKey, fingerprint, tooling.Hash(input), input, tooling.JSON(snap), req.Mode, now, deadline, s.toolLane())
	if err != nil {
		return nil, err
	}
	for i, step := range agenting.Steps {
		if _, err = tx.Exec(ctx, `INSERT INTO agent.agent_steps(agent_run_id,step_id,position,state) VALUES($1,$2,$3,'pending')`, id, step, i+1); err != nil {
			return nil, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(acceptedAgent(id, req.AgentVersionID))}, nil
}
func acceptedAgent(id, version string) agenting.Accepted {
	return agenting.Accepted{AgentRunID: id, AgentVersionID: version, State: "accepted", StatusURL: "/api/agent-runs/" + id}
}
func (s *Store) AgentRun(ctx context.Context, id string) (agenting.Run, error) {
	var r agenting.Run
	if !rpc.ValidID(id) {
		return r, tooling.E("AGENT_INPUT_INVALID")
	}
	var input, snapshot []byte
	var code, stage string
	err := s.Pool.QueryRow(ctx, `SELECT agent_run_id::text,agent_version_id::text,request_id::text,invocation_key::text,input_sha256,input,snapshot,mode,state,tool_call_id::text,result,error_code,error_stage,created_at,started_at,completed_at,deadline_at FROM agent.agent_runs WHERE agent_run_id=$1 AND principal=$2`, id, tooling.Principal).Scan(&r.AgentRunID, &r.AgentVersionID, &r.RequestID, &r.InvocationKey, &r.InputSHA256, &input, &snapshot, &r.Mode, &r.State, &r.ToolCallID, &r.Result, &code, &stage, &r.CreatedAt, &r.StartedAt, &r.CompletedAt, &r.DeadlineAt)
	if errors.Is(err, pgx.ErrNoRows) {
		return r, tooling.E("AGENT_RUN_NOT_FOUND")
	}
	if err != nil {
		return r, err
	}
	var snap agenting.Snapshot
	if err = json.Unmarshal(snapshot, &snap); err != nil {
		return r, err
	}
	if err = json.Unmarshal(input, &r.Input); err != nil {
		return r, err
	}
	r.DefinitionSHA256 = snap.Version.DefinitionSHA256
	r.CallerContext = snap.CallerContext
	if code != "" {
		r.Error = &tooling.CallError{Code: code, Stage: stage, Message: "Agent execution did not complete successfully"}
	}
	if r.StartedAt != nil {
		r.QueueMS = max(0, r.StartedAt.Sub(r.CreatedAt).Milliseconds())
		if r.CompletedAt != nil {
			r.ExecutionMS = max(0, r.CompletedAt.Sub(*r.StartedAt).Milliseconds())
		}
	}
	return r, nil
}
func (s *Store) AgentSteps(ctx context.Context, id string) ([]agenting.Step, error) {
	rows, err := s.Pool.Query(ctx, `SELECT step_id,position,attempt,state,started_at,completed_at,detail FROM agent.agent_steps WHERE agent_run_id=$1 ORDER BY position`, id)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	steps := []agenting.Step{}
	for rows.Next() {
		var step agenting.Step
		if err = rows.Scan(&step.StepID, &step.Position, &step.Attempt, &step.State, &step.StartedAt, &step.CompletedAt, &step.Detail); err != nil {
			return nil, err
		}
		if step.StartedAt != nil && step.CompletedAt != nil {
			step.DurationMS = max(0, step.CompletedAt.Sub(*step.StartedAt).Milliseconds())
		}
		steps = append(steps, step)
	}
	return steps, rows.Err()
}

type AgentJob struct {
	ID, RequestID, LeaseToken string
	Input                     json.RawMessage
	Snapshot                  agenting.Snapshot
	Deadline                  time.Time
}

func (s *Store) ClaimAgent(ctx context.Context) (*AgentJob, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer rollback(tx)
	// A predecessor may have reached a non-idempotent model call. Never replay it.
	if _, err = tx.Exec(ctx, `UPDATE agent.agent_runs SET state='interrupted',error_code='AGENT_EXECUTION_INTERRUPTED',error_stage='execute',completed_at=clock_timestamp() WHERE state='running' AND lease_until<clock_timestamp() AND execution_lane=$1`, s.toolLane()); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE agent.agent_runs SET state='failed',error_code='AGENT_TIMEOUT',error_stage='queue',completed_at=clock_timestamp() WHERE state='accepted' AND deadline_at<=clock_timestamp() AND execution_lane=$1`, s.toolLane()); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE agent.agent_steps st SET state=CASE WHEN st.state='running' THEN 'interrupted' ELSE 'skipped' END,completed_at=clock_timestamp() FROM agent.agent_runs r WHERE r.agent_run_id=st.agent_run_id AND r.state IN ('failed','interrupted') AND st.state IN ('pending','running') AND r.execution_lane=$1`, s.toolLane()); err != nil {
		return nil, err
	}
	var j AgentJob
	var snapshot []byte
	err = tx.QueryRow(ctx, `SELECT agent_run_id::text,request_id::text,input,snapshot,deadline_at FROM agent.agent_runs WHERE state='accepted' AND execution_lane=$1 ORDER BY created_at,agent_run_id FOR UPDATE SKIP LOCKED LIMIT 1`, s.toolLane()).Scan(&j.ID, &j.RequestID, &j.Input, &snapshot, &j.Deadline)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, tx.Commit(ctx)
	}
	if err != nil {
		return nil, err
	}
	if err = tooling.NullableJSON(snapshot, &j.Snapshot, 2<<20); err != nil {
		return nil, err
	}
	j.LeaseToken = rpc.NewID()
	if _, err = tx.Exec(ctx, `UPDATE agent.agent_runs SET state='running',started_at=clock_timestamp(),lease_token=$2,lease_until=LEAST(deadline_at+interval '10 seconds',clock_timestamp()+interval '30 seconds') WHERE agent_run_id=$1`, j.ID, j.LeaseToken); err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &j, nil
}
func (s *Store) HeartbeatAgent(ctx context.Context, j *AgentJob) error {
	tag, err := s.Pool.Exec(ctx, `UPDATE agent.agent_runs SET lease_until=LEAST(deadline_at+interval '10 seconds',clock_timestamp()+interval '30 seconds') WHERE agent_run_id=$1 AND lease_token=$2 AND state='running' AND lease_until>=clock_timestamp()`, j.ID, j.LeaseToken)
	if err == nil && tag.RowsAffected() != 1 {
		return tooling.E("AGENT_EXECUTION_FENCED")
	}
	return err
}

// Commands are authenticated internal RPCs and independently verify the live
// lease and legal step order. No external HTTP request happens in a transaction.
func (s *Store) AgentCommand(ctx context.Context, in *platformv1.AgentCommand) (*platformv1.ToolResponse, error) {
	if !rpc.ValidID(in.AgentRunId) || !rpc.ValidID(in.LeaseToken) {
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer rollback(tx)
	// Same short mutation lock as Tool disable/retire/accept.
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(16100,3)`); err != nil {
		return nil, err
	}
	var snapshot, input []byte
	var child *string
	var deadline time.Time
	var requestID, lane string
	err = tx.QueryRow(ctx, `SELECT snapshot,input,tool_call_id::text,deadline_at,request_id::text,execution_lane FROM agent.agent_runs WHERE agent_run_id=$1 AND lease_token=$2 AND state='running' AND lease_until>=clock_timestamp() AND deadline_at>clock_timestamp() FOR UPDATE`, in.AgentRunId, in.LeaseToken).Scan(&snapshot, &input, &child, &deadline, &requestID, &lane)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, tooling.E("AGENT_EXECUTION_FENCED")
	}
	if err != nil {
		return nil, err
	}
	var position int
	var state string
	var detail []byte
	err = tx.QueryRow(ctx, `SELECT position,state,detail FROM agent.agent_steps WHERE agent_run_id=$1 AND step_id=$2 FOR UPDATE`, in.AgentRunId, in.StepId).Scan(&position, &state, &detail)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	if err != nil {
		return nil, err
	}
	var predecessors bool
	if err = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM agent.agent_steps WHERE agent_run_id=$1 AND position<$2 AND state NOT IN ('succeeded','skipped'))`, in.AgentRunId, position).Scan(&predecessors); err != nil {
		return nil, err
	}
	if predecessors {
		return nil, tooling.E("AGENT_EXECUTION_FENCED")
	}
	switch in.Operation {
	case "start":
		if state != "pending" && state != "running" {
			return nil, tooling.E("AGENT_EXECUTION_FENCED")
		}
		_, err = tx.Exec(ctx, `UPDATE agent.agent_steps SET state='running',started_at=COALESCE(started_at,clock_timestamp()) WHERE agent_run_id=$1 AND step_id=$2`, in.AgentRunId, in.StepId)
	case "finish", "skip":
		if in.Operation == "skip" && in.StepId != "generate" {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		var obj map[string]any
		if tooling.NullableJSON(in.Json, &obj, 64<<10) != nil || obj == nil {
			return nil, tooling.E("AGENT_RESULT_INVALID")
		}
		target := "succeeded"
		if in.Operation == "skip" {
			target = "skipped"
		}
		if state == target {
			var old any
			_ = json.Unmarshal(detail, &old)
			if tooling.Hash(tooling.JSON(old)) != tooling.Hash(tooling.JSON(obj)) {
				return nil, tooling.E("AGENT_EXECUTION_FENCED")
			}
		}
		if state != target && state != "running" && !(in.Operation == "skip" && state == "pending") {
			return nil, tooling.E("AGENT_EXECUTION_FENCED")
		}
		if state != target {
			_, err = tx.Exec(ctx, `UPDATE agent.agent_steps SET state=$3,completed_at=clock_timestamp(),detail=$4 WHERE agent_run_id=$1 AND step_id=$2`, in.AgentRunId, in.StepId, target, tooling.JSON(obj))
		}
	case "search":
		if in.StepId != "search" || state != "running" {
			return nil, tooling.E("AGENT_EXECUTION_FENCED")
		}
		if child == nil {
			var snap agenting.Snapshot
			var args agenting.Input
			if json.Unmarshal(snapshot, &snap) != nil || json.Unmarshal(input, &args) != nil {
				return nil, tooling.E("AGENT_RESULT_INVALID")
			}
			live, e := s.agentTool(ctx, tx, snap.Version.ToolVersionID)
			if e != nil {
				return nil, e
			}
			if live.Version.DefinitionSHA256 != snap.Tool.Version.DefinitionSHA256 {
				return nil, tooling.E("TOOL_VERSION_UNAVAILABLE")
			}
			id, key := rpc.NewID(), rpc.NewID()
			child = &id
			now := tooling.Now()
			end := now.Add(time.Duration(snap.Tool.Version.TimeoutMS) * time.Millisecond)
			if end.After(deadline) {
				end = deadline
			}
			arguments := agenting.Arguments(args)
			req := tooling.CallCreate{ToolVersionID: snap.Version.ToolVersionID, Arguments: arguments, Mode: "run", CallerContext: snap.CallerContext}
			_, err = tx.Exec(ctx, `INSERT INTO agent.tool_calls(tool_call_id,tool_version_id,request_id,principal,invocation_key,fingerprint,input_sha256,mode,state,arguments,snapshot,created_at,deadline_at,execution_lane,parent_agent_run_id,parent_step_id) VALUES($1,$2,$3,$4,$5,$6,$7,'run','accepted',$8,$9,$10,$11,$12,$13,'search')`, id, snap.Version.ToolVersionID, requestID, tooling.Principal, key, tooling.Hash(tooling.JSON(req)), tooling.Hash(arguments), arguments, tooling.JSON(snap.Tool), now, end, lane, in.AgentRunId)
			if err != nil {
				return nil, err
			}
			_, err = tx.Exec(ctx, `UPDATE agent.agent_runs SET tool_call_id=$2 WHERE agent_run_id=$1`, in.AgentRunId, id)
		}
		if err != nil {
			return nil, err
		}
		if err = tx.Commit(ctx); err != nil {
			return nil, err
		}
		call, e := s.ToolCall(ctx, *child)
		if e != nil {
			return nil, e
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(call)}, nil
	default:
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &platformv1.ToolResponse{Json: []byte(`{"ok":true}`)}, nil
}
func (s *Store) FinishAgent(ctx context.Context, j *AgentJob, result []byte, code, stage string) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer rollback(tx)
	var state string
	var priorResult []byte
	var priorCode, priorStage string
	err = tx.QueryRow(ctx, `SELECT state,result,error_code,error_stage FROM agent.agent_runs WHERE agent_run_id=$1 AND lease_token=$2 FOR UPDATE`, j.ID, j.LeaseToken).Scan(&state, &priorResult, &priorCode, &priorStage)
	if errors.Is(err, pgx.ErrNoRows) {
		return tooling.E("AGENT_EXECUTION_FENCED")
	}
	if err != nil {
		return err
	}
	target := "succeeded"
	if code != "" {
		target = "failed"
		result = nil
	}
	if code == "AGENT_EXECUTION_INTERRUPTED" {
		target = "interrupted"
	}
	if state == target && code == priorCode && stage == priorStage {
		var a, b any
		_ = json.Unmarshal(priorResult, &a)
		_ = json.Unmarshal(result, &b)
		if tooling.Hash(tooling.JSON(a)) == tooling.Hash(tooling.JSON(b)) {
			return nil
		}
	}
	if state != "running" {
		return tooling.E("AGENT_EXECUTION_FENCED")
	}
	if code == "" {
		if err = s.validateAgentResult(ctx, tx, j, result); err != nil {
			code, stage, target = "AGENT_RESULT_INVALID", "finalize", "failed"
			result = nil
		}
	}
	tag, err := tx.Exec(ctx, `UPDATE agent.agent_runs SET state=$3,result=$4,error_code=$5,error_stage=$6,completed_at=clock_timestamp() WHERE agent_run_id=$1 AND lease_token=$2 AND state='running' AND lease_until>=clock_timestamp()`, j.ID, j.LeaseToken, target, result, code, stage)
	if err != nil {
		return err
	}
	if tag.RowsAffected() != 1 {
		return tooling.E("AGENT_EXECUTION_FENCED")
	}
	if code != "" {
		_, err = tx.Exec(ctx, `UPDATE agent.agent_steps SET state=CASE WHEN state='running' THEN $2 ELSE 'skipped' END,completed_at=clock_timestamp(),detail=CASE WHEN state='running' THEN jsonb_build_object('error_code',$3::text) ELSE detail END WHERE agent_run_id=$1 AND state IN ('running','pending')`, j.ID, target, code)
		if err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
