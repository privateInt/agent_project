package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"time"
)

func (s *Store) acceptTool(ctx context.Context, tx pgx.Tx, in *platformv1.ToolRequest, raw []byte) (*platformv1.ToolResponse, error) {
	var req tooling.CallCreate
	if err := tooling.NullableJSON(raw, &req, tooling.MaxInput); err != nil {
		return nil, err
	}
	var envelope map[string]json.RawMessage
	_ = json.Unmarshal(raw, &envelope)
	for key, val := range envelope {
		if key != "arguments" {
			var check any
			if tooling.StrictJSON(val, &check) != nil {
				return nil, tooling.E("TOOL_INPUT_INVALID")
			}
		}
	}
	if req.Mode == "" {
		req.Mode = "run"
	}
	if !rpc.ValidID(req.ToolVersionID) || (req.Mode != "run" && req.Mode != "test") {
		return nil, tooling.E("TOOL_INPUT_INVALID")
	}
	for _, v := range []string{req.CallerContext.System, req.CallerContext.WorkflowID, req.CallerContext.ExecutionID, req.CallerContext.NodeID} {
		if !tooling.ValidLabel(v, 200, false) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
	}
	var parsed any
	err := tooling.NullableJSON(req.Arguments, &parsed, tooling.MaxInput)
	args := tooling.JSON(parsed)
	if err != nil {
		return nil, err
	}
	req.Arguments = args
	fingerprint := tooling.Hash(tooling.JSON(req))
	var oldHash, oldID, oldVersion string
	err = tx.QueryRow(ctx, `SELECT fingerprint,tool_call_id::text,tool_version_id::text FROM agent.tool_calls WHERE principal=$1 AND invocation_key=$2`, tooling.Principal, in.IdempotencyKey).Scan(&oldHash, &oldID, &oldVersion)
	if err == nil {
		if fingerprint != oldHash {
			return nil, tooling.E("KEY_CONFLICT")
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(accepted(oldID, oldVersion)), Replayed: true}, nil
	}
	if !errors.Is(err, pgx.ErrNoRows) {
		return nil, err
	}
	var v tooling.ToolVersion
	if err = loadDocument(ctx, tx, "tool_versions", "tool_version_id", req.ToolVersionID, &v); err != nil {
		return nil, err
	}
	var t tooling.Tool
	if err = loadDocument(ctx, tx, "tools", "tool_id", v.ToolID, &t); err != nil {
		return nil, err
	}
	if !t.Enabled || v.State == "retired" || (req.Mode == "run" && v.State != "published") || (req.Mode == "test" && v.State != "draft") {
		return nil, tooling.E("TOOL_VERSION_UNAVAILABLE")
	}
	if err = tooling.ValidateArguments(v, args); err != nil {
		return nil, err
	}
	snapshot := tooling.Snapshot{Version: v, CallerContext: req.CallerContext}
	if v.Binding.ConnectionVersionID != "" {
		var cv tooling.ConnectionVersion
		if err = loadDocument(ctx, tx, "connection_versions", "connection_version_id", v.Binding.ConnectionVersionID, &cv); err != nil {
			return nil, err
		}
		snapshot.Connection = &cv
	}
	if v.Binding.Knowledge != nil {
		var cv tooling.ConnectionVersion
		if err = loadDocument(ctx, tx, "connection_versions", "connection_version_id", v.Binding.Knowledge.ModelConnectionVersionID, &cv); err != nil {
			return nil, err
		}
		snapshot.ModelConnection = &cv
	}
	id := rpc.NewID()
	now := tooling.Now()
	deadline := now.Add(time.Duration(v.TimeoutMS) * time.Millisecond)
	_, err = tx.Exec(ctx, `INSERT INTO agent.tool_calls(tool_call_id,tool_version_id,request_id,principal,invocation_key,fingerprint,input_sha256,mode,state,arguments,snapshot,created_at,deadline_at,execution_lane) VALUES($1,$2,$3,$4,$5,$6,$7,$8,'accepted',$9,$10,$11,$12,$13)`, id, v.ToolVersionID, rpc.RequestID(ctx), tooling.Principal, in.IdempotencyKey, fingerprint, tooling.Hash(args), req.Mode, args, tooling.JSON(snapshot), now, deadline, s.toolLane())
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(accepted(id, v.ToolVersionID))}, nil
}
func accepted(id, version string) tooling.Accepted {
	return tooling.Accepted{ToolCallID: id, ToolVersionID: version, State: "accepted", StatusURL: "/api/tool-calls/" + id}
}
func (s *Store) ToolCall(ctx context.Context, id string) (tooling.Call, error) {
	var c tooling.Call
	if !rpc.ValidID(id) {
		return c, tooling.E("TOOL_INPUT_INVALID")
	}
	var errorCode string
	var snapshot []byte
	err := s.Pool.QueryRow(ctx, `SELECT tool_call_id::text,tool_version_id::text,request_id::text,invocation_key::text,input_sha256,mode,state,result,error_code,created_at,started_at,completed_at,deadline_at,snapshot,parent_agent_run_id::text,parent_step_id FROM agent.tool_calls WHERE tool_call_id=$1 AND principal=$2`, id, tooling.Principal).Scan(&c.ToolCallID, &c.ToolVersionID, &c.RequestID, &c.InvocationKey, &c.InputSHA256, &c.Mode, &c.State, &c.Result, &errorCode, &c.CreatedAt, &c.StartedAt, &c.CompletedAt, &c.DeadlineAt, &snapshot, &c.ParentAgentRunID, &c.ParentStepID)
	if errors.Is(err, pgx.ErrNoRows) {
		return c, tooling.E("TOOL_CALL_NOT_FOUND")
	}
	if err != nil {
		return c, err
	}
	var snap tooling.Snapshot
	if err = json.Unmarshal(snapshot, &snap); err != nil {
		return c, err
	}
	c.DefinitionSHA256 = snap.Version.DefinitionSHA256
	c.CallerContext = snap.CallerContext
	if errorCode != "" {
		c.Error = &tooling.CallError{Code: errorCode, Message: "Tool execution did not complete successfully", Stage: "execute"}
		if snap.Version.Kind == "knowledge_search_v1" {
			var failure struct {
				Stage string `json:"stage"`
			}
			if json.Unmarshal(c.Result, &failure) == nil && tooling.KnowledgeStage(failure.Stage) {
				c.Error.Stage = failure.Stage
			}
		}
	}
	if c.StartedAt != nil {
		c.QueueMS = max(0, c.StartedAt.Sub(c.CreatedAt).Milliseconds())
		if c.CompletedAt != nil {
			c.ExecutionMS = max(0, c.CompletedAt.Sub(*c.StartedAt).Milliseconds())
		}
	}
	return c, nil
}

type ToolJob struct {
	ID, RequestID, LeaseToken, InvocationKey string
	ParentAgentRunID, ParentStepID           *string
	Arguments                                json.RawMessage
	Snapshot                                 tooling.Snapshot
	Deadline                                 time.Time
}

func (s *Store) ClaimTool(ctx context.Context) (*ToolJob, error) {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer rollback(tx)
	// Never retry an execution whose previous owner may already have acted.
	if _, err = tx.Exec(ctx, `UPDATE agent.tool_calls SET state=CASE WHEN snapshot->'version'->>'kind'='openapi_http_v1' AND snapshot->'version'->'binding'->>'effect'='write' THEN 'unknown' ELSE 'interrupted' END,error_code=CASE WHEN snapshot->'version'->>'kind'='openapi_http_v1' AND snapshot->'version'->'binding'->>'effect'='write' THEN 'TOOL_EFFECT_UNKNOWN' ELSE 'TOOL_EXECUTION_INTERRUPTED' END,completed_at=clock_timestamp() WHERE state='running' AND lease_until<clock_timestamp() AND execution_lane=$1`, s.toolLane()); err != nil {
		return nil, err
	}
	if _, err = tx.Exec(ctx, `UPDATE agent.tool_calls SET state='failed',error_code='TOOL_TIMEOUT',completed_at=clock_timestamp() WHERE state='accepted' AND deadline_at<=clock_timestamp() AND execution_lane=$1`, s.toolLane()); err != nil {
		return nil, err
	}
	var j ToolJob
	var snapshot []byte
	err = tx.QueryRow(ctx, `SELECT tool_call_id::text,request_id::text,arguments,snapshot,deadline_at,invocation_key::text,parent_agent_run_id::text,parent_step_id FROM agent.tool_calls WHERE state='accepted' AND execution_lane=$1 ORDER BY created_at,tool_call_id FOR UPDATE SKIP LOCKED LIMIT 1`, s.toolLane()).Scan(&j.ID, &j.RequestID, &j.Arguments, &snapshot, &j.Deadline, &j.InvocationKey, &j.ParentAgentRunID, &j.ParentStepID)
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
	_, err = tx.Exec(ctx, `UPDATE agent.tool_calls SET state='running',started_at=clock_timestamp(),lease_token=$2,lease_until=deadline_at+interval '10 seconds' WHERE tool_call_id=$1`, j.ID, j.LeaseToken)
	if err != nil {
		return nil, err
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &j, nil
}
func (s *Store) FinishTool(ctx context.Context, j *ToolJob, result []byte, errorCode string) error {
	state := "succeeded"
	if errorCode != "" {
		state = "failed"
		if errorCode == "TOOL_EFFECT_UNKNOWN" {
			state = "unknown"
		} else if errorCode == "TOOL_ASYNC_UNSUPPORTED" {
			state = "external_pending"
		} else if j.Snapshot.Version.Kind != "knowledge_search_v1" && (j.Snapshot.Version.Kind != "openapi_http_v1" || errorCode != "TOOL_HTTP_ERROR") {
			result = nil
		}
	}
	tag, err := s.Pool.Exec(ctx, `UPDATE agent.tool_calls SET state=$3,result=$4,error_code=$5,completed_at=clock_timestamp() WHERE tool_call_id=$1 AND lease_token=$2 AND state='running' AND lease_until>=clock_timestamp()`, j.ID, j.LeaseToken, state, result, errorCode)
	if err != nil {
		return err
	}
	if tag.RowsAffected() == 0 {
		return tooling.E("TOOL_EXECUTION_FENCED")
	}
	return nil
}

func (s *Store) toolLane() string {
	if s.ToolLane != "" {
		return s.ToolLane
	}
	return "live"
}
