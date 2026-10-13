package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"strings"
)

type queryer interface {
	QueryRow(context.Context, string, ...any) pgx.Row
}

func loadDocument(ctx context.Context, q queryer, table, key, id string, out any) error {
	var raw []byte
	err := q.QueryRow(ctx, "SELECT document FROM agent."+table+" WHERE "+key+"=$1", id).Scan(&raw)
	if errors.Is(err, pgx.ErrNoRows) {
		if table == "workflow_links" {
			return tooling.E("WORKFLOW_LINK_NOT_FOUND")
		}
		if table == "agents" || table == "agent_versions" {
			return tooling.E("AGENT_NOT_FOUND")
		}
		if strings.HasPrefix(table, "connection") {
			return tooling.E("CONNECTION_NOT_FOUND")
		}
		return tooling.E("TOOL_NOT_FOUND")
	}
	if err != nil {
		return err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	return decoder.Decode(out)
}
func (s *Store) ToolAPI(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	reads := map[string]bool{"GetOpenAPIImport": true, "ListOpenAPIImports": true, "GetTool": true, "GetToolVersion": true, "GetConnection": true, "GetConnectionVersion": true, "GetToolCall": true, "ListTools": true, "ListConnections": true, "ListMCPTools": true}
	if in.ResourceId != "" && !rpc.ValidID(in.ResourceId) {
		return nil, tooling.E("TOOL_INPUT_INVALID")
	}
	if op == "GetAgent" || op == "GetAgentVersion" || op == "ListAgents" || op == "GetAgentRun" || op == "ListAgentRuns" || op == "GetAgentSteps" {
		return s.readAgentAPI(ctx, op, in)
	}
	if op == "GetWorkflowLink" || op == "ListWorkflowLinks" {
		return s.readWorkflow(ctx, op, in)
	}
	if reads[op] {
		return s.readToolAPI(ctx, op, in)
	}
	if op != "UpdateTool" && op != "UpdateAgent" && op != "UpdateWorkflowLink" && !rpc.ValidID(in.IdempotencyKey) {
		return nil, tooling.E("IDEMPOTENCY_KEY_REQUIRED")
	}
	if op == "CreateOpenAPIImport" {
		return s.createImport(ctx, in)
	}
	var canonical []byte
	var err error
	if op == "CreateToolCall" {
		var v any
		err = tooling.NullableJSON(in.Json, &v, tooling.MaxInput)
		if err == nil {
			canonical = tooling.JSON(v)
		}
	} else {
		canonical, err = tooling.Canonical(in.Json)
	}
	if err != nil {
		return nil, err
	}
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return nil, err
	}
	defer rollback(tx)
	// Mutations and accept checks share a short transaction lock. Workers never
	// hold this lock while executing. This serializes publication/disable/accept.
	if _, err = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(16100,3)"); err != nil {
		return nil, err
	}
	if op == "CreateAgentRun" {
		return s.acceptAgent(ctx, tx, in, canonical)
	}
	if op == "CreateToolCall" {
		return s.acceptTool(ctx, tx, in, canonical)
	}
	hash := tooling.Hash(canonical)
	if op != "UpdateTool" && op != "UpdateAgent" && op != "UpdateWorkflowLink" {
		var oldHash string
		var response []byte
		err = tx.QueryRow(ctx, `SELECT fingerprint,response FROM agent.tool_idempotency WHERE principal=$1 AND operation=$2 AND resource_id=$3 AND key=$4`, tooling.Principal, op, in.ResourceId, in.IdempotencyKey).Scan(&oldHash, &response)
		if err == nil {
			if oldHash != hash {
				return nil, tooling.E("KEY_CONFLICT")
			}
			return &platformv1.ToolResponse{Json: response, Replayed: true}, nil
		}
		if !errors.Is(err, pgx.ErrNoRows) {
			return nil, err
		}
	}
	response, err := s.mutateTool(ctx, tx, op, in.ResourceId, canonical)
	if err != nil {
		return nil, err
	}
	raw := tooling.JSON(response)
	if op != "UpdateTool" && op != "UpdateAgent" && op != "UpdateWorkflowLink" {
		if _, err = tx.Exec(ctx, `INSERT INTO agent.tool_idempotency(principal,operation,resource_id,key,fingerprint,response) VALUES($1,$2,$3,$4,$5,$6)`, tooling.Principal, op, in.ResourceId, in.IdempotencyKey, hash, raw); err != nil {
			return nil, err
		}
	}
	if err = tx.Commit(ctx); err != nil {
		return nil, err
	}
	return &platformv1.ToolResponse{Json: raw}, nil
}
func (s *Store) mutateTool(ctx context.Context, tx pgx.Tx, op, id string, raw []byte) (any, error) {
	if strings.Contains(op, "WorkflowLink") {
		return s.mutateWorkflow(ctx, tx, op, id, raw)
	}
	if strings.Contains(op, "Agent") {
		return s.mutateAgent(ctx, tx, op, id, raw)
	}
	switch op {
	case "CreateTool":
		var in tooling.ToolCreate
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		if !tooling.ValidLabel(in.DisplayName, 200, true) || !tooling.ValidLabel(in.Description, 4000, false) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
		now := tooling.Now()
		v := tooling.Tool{ToolID: rpc.NewID(), DisplayName: in.DisplayName, Description: in.Description, Enabled: true, Revision: 1, CreatedAt: now, UpdatedAt: now}
		_, err := tx.Exec(ctx, "INSERT INTO agent.tools(tool_id,revision,document) VALUES($1,$2,$3)", v.ToolID, v.Revision, tooling.JSON(v))
		return v, err
	case "UpdateTool":
		var in tooling.ToolPatch
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		if in.ExpectedRevision < 1 || (in.DisplayName == nil && in.Description == nil && in.Enabled == nil) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
		var v tooling.Tool
		if err := loadDocument(ctx, tx, "tools", "tool_id", id, &v); err != nil {
			return nil, err
		}
		if v.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if in.DisplayName != nil {
			if !tooling.ValidLabel(*in.DisplayName, 200, true) {
				return nil, tooling.E("TOOL_INPUT_INVALID")
			}
			v.DisplayName = *in.DisplayName
		}
		if in.Description != nil {
			if !tooling.ValidLabel(*in.Description, 4000, false) {
				return nil, tooling.E("TOOL_INPUT_INVALID")
			}
			v.Description = *in.Description
		}
		if in.Enabled != nil {
			v.Enabled = *in.Enabled
		}
		v.Revision++
		v.UpdatedAt = tooling.Now()
		_, err := tx.Exec(ctx, "UPDATE agent.tools SET revision=$2,document=$3 WHERE tool_id=$1", id, v.Revision, tooling.JSON(v))
		return v, err
	case "CreateToolVersion":
		var in tooling.VersionCreate
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		if in.TimeoutMS == 0 {
			in.TimeoutMS = 30000
			if in.Kind == "knowledge_search_v1" {
				in.TimeoutMS = 180000
				if in.Binding.Knowledge != nil && in.Binding.Knowledge.Rerank != nil {
					in.TimeoutMS = 240000
				}
			}
		}
		if in.Kind == "openapi_http_v1" && in.Binding.Effect == "" {
			in.Binding.Effect = "write"
		}
		if err := tooling.ValidateVersion(in); err != nil {
			return nil, err
		}
		var t tooling.Tool
		if err := loadDocument(ctx, tx, "tools", "tool_id", id, &t); err != nil {
			return nil, err
		}
		if t.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if in.Kind == "diagnostic_connection_v1" {
			if !rpc.ValidID(in.Binding.ConnectionVersionID) {
				return nil, tooling.E("TOOL_INPUT_INVALID")
			}
			var c tooling.ConnectionVersion
			if err := loadDocument(ctx, tx, "connection_versions", "connection_version_id", in.Binding.ConnectionVersionID, &c); err != nil {
				return nil, err
			}
		}
		if in.Kind == "knowledge_search_v1" {
			for _, cid := range []string{in.Binding.ConnectionVersionID, in.Binding.Knowledge.ModelConnectionVersionID} {
				var c tooling.ConnectionVersion
				if err := loadDocument(ctx, tx, "connection_versions", "connection_version_id", cid, &c); err != nil {
					return nil, err
				}
			}
		}
		input, output, err := tooling.Schemas(in.Kind)
		if in.Kind == "openapi_http_v1" {
			input, output, err = s.compileHTTP(ctx, tx, &in)
		}
		if err != nil {
			return nil, err
		}
		var n int
		if err = tx.QueryRow(ctx, "SELECT COALESCE(max(version),0)+1 FROM agent.tool_versions WHERE tool_id=$1", id).Scan(&n); err != nil {
			return nil, err
		}
		if n > 100 {
			return nil, tooling.E("TOOL_LIMIT_REACHED")
		}
		v := tooling.ToolVersion{ToolVersionID: rpc.NewID(), ToolID: id, Version: n, Revision: 1, State: "draft", Kind: in.Kind, InputSchema: input, OutputSchema: output, Binding: in.Binding, TimeoutMS: in.TimeoutMS, CreatedAt: tooling.Now()}
		v.DefinitionSHA256 = tooling.Hash(tooling.JSON(map[string]any{"kind": v.Kind, "input_schema": v.InputSchema, "output_schema": v.OutputSchema, "binding": v.Binding, "timeout_ms": v.TimeoutMS}))
		if _, err = tx.Exec(ctx, "INSERT INTO agent.tool_versions(tool_version_id,tool_id,version,revision,state,document) VALUES($1,$2,$3,$4,$5,$6)", v.ToolVersionID, id, v.Version, v.Revision, v.State, tooling.JSON(v)); err != nil {
			return nil, err
		}
		t.Revision++
		t.UpdatedAt = tooling.Now()
		_, err = tx.Exec(ctx, "UPDATE agent.tools SET revision=$2,document=$3 WHERE tool_id=$1", id, t.Revision, tooling.JSON(t))
		return v, err
	case "PublishToolVersion", "RetireToolVersion":
		var in tooling.Revision
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		var v tooling.ToolVersion
		if err := loadDocument(ctx, tx, "tool_versions", "tool_version_id", id, &v); err != nil {
			return nil, err
		}
		if in.ExpectedRevision != v.Revision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if op == "PublishToolVersion" {
			if v.State != "draft" {
				return nil, tooling.E("TOOL_VERSION_UNAVAILABLE")
			}
			var count int
			if err := tx.QueryRow(ctx, "SELECT count(*) FROM agent.tool_versions WHERE state IN ('published','retired')").Scan(&count); err != nil {
				return nil, err
			}
			if count >= 1000 {
				return nil, tooling.E("TOOL_LIMIT_REACHED")
			}
			if v.Kind == "openapi_http_v1" || v.Kind == "knowledge_search_v1" {
				var ok bool
				if e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM agent.tool_calls WHERE tool_version_id=$1 AND mode='test' AND state='succeeded')`, id).Scan(&ok); e != nil {
					return nil, e
				}
				if !ok {
					return nil, tooling.E("TOOL_TEST_REQUIRED")
				}
			}
			v.State = "published"
		} else {
			if v.State != "published" {
				return nil, tooling.E("TOOL_VERSION_UNAVAILABLE")
			}
			v.State = "retired"
		}
		v.Revision++
		_, err := tx.Exec(ctx, "UPDATE agent.tool_versions SET revision=$2,state=$3,document=$4 WHERE tool_version_id=$1", id, v.Revision, v.State, tooling.JSON(v))
		return v, err
	case "CreateConnection":
		var in tooling.ConnectionCreate
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		if !tooling.ValidLabel(in.DisplayName, 200, true) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
		if err := tooling.ValidateConnection(in.ConnectionSpec); err != nil {
			return nil, err
		}
		now := tooling.Now()
		v := tooling.Connection{ConnectionID: rpc.NewID(), DisplayName: in.DisplayName, Revision: 1, CreatedAt: now, CurrentVersionID: rpc.NewID()}
		cv := tooling.ConnectionVersion{ConnectionVersionID: v.CurrentVersionID, ConnectionID: v.ConnectionID, Version: 1, ConnectionSpec: in.ConnectionSpec, CreatedAt: now}
		if _, err := tx.Exec(ctx, "INSERT INTO agent.connections(connection_id,revision,document) VALUES($1,$2,$3)", v.ConnectionID, v.Revision, tooling.JSON(v)); err != nil {
			return nil, err
		}
		_, err := tx.Exec(ctx, "INSERT INTO agent.connection_versions(connection_version_id,connection_id,version,document) VALUES($1,$2,$3,$4)", cv.ConnectionVersionID, cv.ConnectionID, cv.Version, tooling.JSON(cv))
		return v, err
	case "CreateConnectionVersion":
		var in tooling.ConnectionVersionCreate
		if err := tooling.StrictJSON(raw, &in); err != nil {
			return nil, err
		}
		if err := tooling.ValidateConnection(in.ConnectionSpec); err != nil {
			return nil, err
		}
		var v tooling.Connection
		if err := loadDocument(ctx, tx, "connections", "connection_id", id, &v); err != nil {
			return nil, err
		}
		if v.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if v.Revision >= 100 {
			return nil, tooling.E("TOOL_LIMIT_REACHED")
		}
		v.Revision++
		v.CurrentVersionID = rpc.NewID()
		cv := tooling.ConnectionVersion{ConnectionVersionID: v.CurrentVersionID, ConnectionID: id, Version: v.Revision, ConnectionSpec: in.ConnectionSpec, CreatedAt: tooling.Now()}
		if _, err := tx.Exec(ctx, "INSERT INTO agent.connection_versions(connection_version_id,connection_id,version,document) VALUES($1,$2,$3,$4)", cv.ConnectionVersionID, id, cv.Version, tooling.JSON(cv)); err != nil {
			return nil, err
		}
		_, err := tx.Exec(ctx, "UPDATE agent.connections SET revision=$2,document=$3 WHERE connection_id=$1", id, v.Revision, tooling.JSON(v))
		return cv, err
	default:
		return nil, tooling.E("TOOL_INPUT_INVALID")
	}
}
func (s *Store) readToolAPI(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	if op == "GetOpenAPIImport" || op == "ListOpenAPIImports" {
		return s.readImport(ctx, op, in)
	}
	if op == "GetToolCall" {
		v, err := s.ToolCall(ctx, in.ResourceId)
		if err != nil {
			return nil, err
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(v)}, nil
	}
	if op == "GetTool" || op == "GetToolVersion" || op == "GetConnection" || op == "GetConnectionVersion" {
		if !rpc.ValidID(in.ResourceId) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
		tables := map[string][2]string{"GetTool": {"tools", "tool_id"}, "GetToolVersion": {"tool_versions", "tool_version_id"}, "GetConnection": {"connections", "connection_id"}, "GetConnectionVersion": {"connection_versions", "connection_version_id"}}
		spec := tables[op]
		var v map[string]any
		if err := loadDocument(ctx, s.Pool, spec[0], spec[1], in.ResourceId, &v); err != nil {
			return nil, err
		}
		if op == "GetTool" || op == "GetConnection" {
			table, key := "tool_versions", "tool_id"
			if op == "GetConnection" {
				table, key = "connection_versions", "connection_id"
			}
			rows, err := s.Pool.Query(ctx, "SELECT document FROM agent."+table+" WHERE "+key+"=$1 ORDER BY version", in.ResourceId)
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
			v["versions"] = versions
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(v)}, nil
	}
	limit := int(in.Limit)
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 || in.Cursor != "" && !rpc.ValidID(in.Cursor) {
		return nil, tooling.E("TOOL_INPUT_INVALID")
	}
	cursor := in.Cursor
	if cursor == "" {
		cursor = "00000000-0000-0000-0000-000000000000"
	}
	page := tooling.Page{Items: []json.RawMessage{}}
	ids := []string{}
	var rows pgx.Rows
	var err error
	if op == "ListMCPTools" {
		rows, err = s.Pool.Query(ctx, `SELECT v.tool_version_id::text,v.document,t.document FROM agent.tool_versions v JOIN agent.tools t USING(tool_id) WHERE v.state IN ('published','retired') AND v.tool_version_id>$1 ORDER BY v.tool_version_id LIMIT $2`, cursor, limit+1)
	} else {
		table, key := "tools", "tool_id"
		if op == "ListConnections" {
			table, key = "connections", "connection_id"
		}
		rows, err = s.Pool.Query(ctx, "SELECT "+key+"::text,document FROM agent."+table+" WHERE "+key+">$1 ORDER BY "+key+" LIMIT $2", cursor, limit+1)
	}
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	for rows.Next() {
		var id string
		var doc, toolDoc []byte
		if op == "ListMCPTools" {
			if err = rows.Scan(&id, &doc, &toolDoc); err != nil {
				return nil, err
			}
			var v tooling.ToolVersion
			var t tooling.Tool
			if json.Unmarshal(doc, &v) != nil || json.Unmarshal(toolDoc, &t) != nil {
				return nil, errors.New("invalid stored Tool")
			}
			doc = tooling.JSON(tooling.MCPEntry{Name: fmt.Sprintf("t_%s_v%d", strings.ReplaceAll(t.ToolID, "-", ""), v.Version), DisplayName: t.DisplayName, Description: t.Description, Available: t.Enabled && v.State == "published", Version: v})
		} else {
			if err = rows.Scan(&id, &doc); err != nil {
				return nil, err
			}
		}
		ids = append(ids, id)
		page.Items = append(page.Items, doc)
	}
	if err = rows.Err(); err != nil {
		return nil, err
	}
	if len(page.Items) > limit {
		page.NextCursor = ids[limit-1]
		page.Items = page.Items[:limit]
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(page)}, nil
}
