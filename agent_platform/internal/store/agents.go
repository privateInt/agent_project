package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
)

func (s *Store) agentTool(ctx context.Context, q queryer, id string) (tooling.Snapshot, error) {
	var snap tooling.Snapshot
	if err := loadDocument(ctx, q, "tool_versions", "tool_version_id", id, &snap.Version); err != nil {
		return snap, err
	}
	var t tooling.Tool
	if err := loadDocument(ctx, q, "tools", "tool_id", snap.Version.ToolID, &t); err != nil {
		return snap, err
	}
	if !t.Enabled || snap.Version.State != "published" || snap.Version.Kind != "knowledge_search_v1" {
		return snap, tooling.E("TOOL_VERSION_UNAVAILABLE")
	}
	snap.Connection = &tooling.ConnectionVersion{}
	snap.ModelConnection = &tooling.ConnectionVersion{}
	if err := loadDocument(ctx, q, "connection_versions", "connection_version_id", snap.Version.Binding.ConnectionVersionID, snap.Connection); err != nil {
		return snap, err
	}
	if err := loadDocument(ctx, q, "connection_versions", "connection_version_id", snap.Version.Binding.Knowledge.ModelConnectionVersionID, snap.ModelConnection); err != nil {
		return snap, err
	}
	return snap, nil
}
func (s *Store) mutateAgent(ctx context.Context, tx pgx.Tx, op, id string, raw []byte) (any, error) {
	switch op {
	case "CreateAgent":
		var in tooling.ToolCreate
		if tooling.StrictJSON(raw, &in) != nil || !tooling.ValidLabel(in.DisplayName, 200, true) || !tooling.ValidLabel(in.Description, 4000, false) {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		now := tooling.Now()
		v := agenting.Agent{AgentID: rpc.NewID(), DisplayName: in.DisplayName, Description: in.Description, Enabled: true, Revision: 1, CreatedAt: now, UpdatedAt: now}
		_, err := tx.Exec(ctx, `INSERT INTO agent.agents(agent_id,revision,document) VALUES($1,$2,$3)`, v.AgentID, v.Revision, tooling.JSON(v))
		return v, err
	case "UpdateAgent":
		var in tooling.ToolPatch
		if tooling.StrictJSON(raw, &in) != nil || in.ExpectedRevision < 1 || (in.DisplayName == nil && in.Description == nil && in.Enabled == nil) {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		var v agenting.Agent
		if err := loadDocument(ctx, tx, "agents", "agent_id", id, &v); err != nil {
			return nil, err
		}
		if v.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if in.DisplayName != nil {
			if !tooling.ValidLabel(*in.DisplayName, 200, true) {
				return nil, tooling.E("AGENT_INPUT_INVALID")
			}
			v.DisplayName = *in.DisplayName
		}
		if in.Description != nil {
			if !tooling.ValidLabel(*in.Description, 4000, false) {
				return nil, tooling.E("AGENT_INPUT_INVALID")
			}
			v.Description = *in.Description
		}
		if in.Enabled != nil {
			v.Enabled = *in.Enabled
		}
		v.Revision++
		v.UpdatedAt = tooling.Now()
		_, err := tx.Exec(ctx, `UPDATE agent.agents SET revision=$2,document=$3 WHERE agent_id=$1`, id, v.Revision, tooling.JSON(v))
		return v, err
	case "CreateAgentVersion":
		var in agenting.VersionCreate
		if tooling.StrictJSON(raw, &in) != nil {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		agenting.Defaults(&in)
		if err := agenting.ValidateVersion(in); err != nil {
			return nil, err
		}
		var a agenting.Agent
		if err := loadDocument(ctx, tx, "agents", "agent_id", id, &a); err != nil {
			return nil, err
		}
		if a.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if _, err := s.agentTool(ctx, tx, in.ToolVersionID); err != nil {
			return nil, err
		}
		var conn tooling.ConnectionVersion
		if err := loadDocument(ctx, tx, "connection_versions", "connection_version_id", in.Model.ConnectionVersionID, &conn); err != nil {
			return nil, err
		}
		var n int
		if err := tx.QueryRow(ctx, `SELECT COALESCE(max(version),0)+1 FROM agent.agent_versions WHERE agent_id=$1`, id).Scan(&n); err != nil {
			return nil, err
		}
		if n > 100 {
			return nil, tooling.E("AGENT_LIMIT_REACHED")
		}
		v := agenting.Version{AgentVersionID: rpc.NewID(), AgentID: id, Version: n, Revision: 1, State: "draft", Definition: in.Definition, InputSchema: agenting.InputSchema(), OutputSchema: agenting.OutputSchema(), CreatedAt: tooling.Now()}
		v.DefinitionSHA256 = tooling.Hash(tooling.JSON(map[string]any{"definition": v.Definition, "input_schema": v.InputSchema, "output_schema": v.OutputSchema}))
		if _, err := tx.Exec(ctx, `INSERT INTO agent.agent_versions(agent_version_id,agent_id,version,revision,state,tool_version_id,document) VALUES($1,$2,$3,$4,$5,$6,$7)`, v.AgentVersionID, id, n, v.Revision, v.State, v.ToolVersionID, tooling.JSON(v)); err != nil {
			return nil, err
		}
		a.Revision++
		a.UpdatedAt = tooling.Now()
		_, err := tx.Exec(ctx, `UPDATE agent.agents SET revision=$2,document=$3 WHERE agent_id=$1`, id, a.Revision, tooling.JSON(a))
		return v, err
	case "PublishAgentVersion", "RetireAgentVersion":
		var in tooling.Revision
		if tooling.StrictJSON(raw, &in) != nil {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		var v agenting.Version
		if err := loadDocument(ctx, tx, "agent_versions", "agent_version_id", id, &v); err != nil {
			return nil, err
		}
		if v.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if op == "PublishAgentVersion" {
			if v.State != "draft" {
				return nil, tooling.E("AGENT_VERSION_UNAVAILABLE")
			}
			var a agenting.Agent
			if err := loadDocument(ctx, tx, "agents", "agent_id", v.AgentID, &a); err != nil {
				return nil, err
			}
			if !a.Enabled {
				return nil, tooling.E("AGENT_VERSION_UNAVAILABLE")
			}
			if _, err := s.agentTool(ctx, tx, v.ToolVersionID); err != nil {
				return nil, err
			}
			var ok bool
			if err := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM agent.agent_runs WHERE agent_version_id=$1 AND mode='test' AND state='succeeded')`, id).Scan(&ok); err != nil {
				return nil, err
			}
			if !ok {
				return nil, tooling.E("AGENT_TEST_REQUIRED")
			}
			v.State = "published"
		} else {
			if v.State != "published" {
				return nil, tooling.E("AGENT_VERSION_UNAVAILABLE")
			}
			v.State = "retired"
		}
		v.Revision++
		_, err := tx.Exec(ctx, `UPDATE agent.agent_versions SET revision=$2,state=$3,document=$4 WHERE agent_version_id=$1`, id, v.Revision, v.State, tooling.JSON(v))
		return v, err
	}
	return nil, tooling.E("AGENT_INPUT_INVALID")
}
func (s *Store) readAgentAPI(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	if op == "GetAgentRun" {
		v, err := s.AgentRun(ctx, in.ResourceId)
		if err != nil {
			return nil, err
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(v)}, nil
	}
	if op == "GetAgentSteps" {
		if _, err := s.AgentRun(ctx, in.ResourceId); err != nil {
			return nil, err
		}
		steps, err := s.AgentSteps(ctx, in.ResourceId)
		if err != nil {
			return nil, err
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(map[string]any{"items": steps})}, nil
	}
	if op == "GetAgent" || op == "GetAgentVersion" {
		if !rpc.ValidID(in.ResourceId) {
			return nil, tooling.E("AGENT_INPUT_INVALID")
		}
		table, key := "agents", "agent_id"
		if op == "GetAgentVersion" {
			table, key = "agent_versions", "agent_version_id"
		}
		var obj map[string]any
		if err := loadDocument(ctx, s.Pool, table, key, in.ResourceId, &obj); err != nil {
			return nil, err
		}
		if op == "GetAgent" {
			rows, err := s.Pool.Query(ctx, `SELECT document FROM agent.agent_versions WHERE agent_id=$1 ORDER BY version`, in.ResourceId)
			if err != nil {
				return nil, err
			}
			defer rows.Close()
			versions := []json.RawMessage{}
			for rows.Next() {
				var doc []byte
				if err = rows.Scan(&doc); err != nil {
					return nil, err
				}
				versions = append(versions, doc)
			}
			if err = rows.Err(); err != nil {
				return nil, err
			}
			obj["versions"] = versions
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(obj)}, nil
	}
	limit := in.Limit
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 || (in.Cursor != "" && !rpc.ValidID(in.Cursor)) {
		return nil, tooling.E("AGENT_INPUT_INVALID")
	}
	cursor := in.Cursor
	if cursor == "" {
		cursor = "00000000-0000-0000-0000-000000000000"
	}
	query := `SELECT agent_id::text,document FROM agent.agents WHERE agent_id>$1 ORDER BY agent_id LIMIT $2`
	if op == "ListAgentRuns" {
		query = `SELECT agent_run_id::text,jsonb_build_object('agent_run_id',agent_run_id,'agent_version_id',agent_version_id,
            'agent_id',snapshot->'version'->>'agent_id','agent_version',snapshot->'version'->'version',
            'state',state,'mode',mode,'created_at',created_at,'completed_at',completed_at,'question',input->>'question',
            'caller_context',snapshot->'caller_context',
            'error',CASE WHEN error_code='' THEN NULL ELSE jsonb_build_object('code',error_code,'stage',error_stage) END)
            FROM agent.agent_runs WHERE agent_run_id>$1 AND principal='agent-api' ORDER BY agent_run_id LIMIT $2`
	}
	rows, err := s.Pool.Query(ctx, query, cursor, limit+1)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	page := tooling.Page{Items: []json.RawMessage{}}
	ids := []string{}
	for rows.Next() {
		var id string
		var raw []byte
		if err = rows.Scan(&id, &raw); err != nil {
			return nil, err
		}
		ids = append(ids, id)
		page.Items = append(page.Items, raw)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	if len(ids) > int(limit) {
		page.Items = page.Items[:limit]
		page.NextCursor = ids[limit-1]
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(page)}, nil
}
