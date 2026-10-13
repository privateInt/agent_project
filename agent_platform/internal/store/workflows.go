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

func (s *Store) mutateWorkflow(ctx context.Context, tx pgx.Tx, op, id string, raw []byte) (any, error) {
	if op == "CreateWorkflowLink" {
		var in agenting.WorkflowLinkCreate
		if tooling.StrictJSON(raw, &in) != nil {
			return nil, tooling.E("WORKFLOW_LINK_INVALID")
		}
		if e := agenting.ValidateWorkflowLink(&in); e != nil {
			return nil, e
		}
		var v agenting.Version
		if e := loadDocument(ctx, tx, "agent_versions", "agent_version_id", in.AgentVersionID, &v); e != nil {
			return nil, e
		}
		var a agenting.Agent
		if e := loadDocument(ctx, tx, "agents", "agent_id", v.AgentID, &a); e != nil {
			return nil, e
		}
		if v.State != "published" || !a.Enabled {
			return nil, tooling.E("AGENT_VERSION_UNAVAILABLE")
		}
		var exists bool
		if e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM agent.workflow_links WHERE instance_url=$1 AND workflow_id=$2)`, in.InstanceURL, in.WorkflowID).Scan(&exists); e != nil {
			return nil, e
		}
		if exists {
			return nil, tooling.E("WORKFLOW_LINK_CONFLICT")
		}
		now := tooling.Now()
		vlink := agenting.WorkflowLink{WorkflowLinkID: rpc.NewID(), WorkflowLinkCreate: in, EditURL: in.InstanceURL + "/workflow/" + in.WorkflowID, Enabled: true, Revision: 1, CreatedAt: now, UpdatedAt: now}
		_, e := tx.Exec(ctx, `INSERT INTO agent.workflow_links(workflow_link_id,agent_version_id,instance_url,workflow_id,revision,document) VALUES($1,$2,$3,$4,$5,$6)`, vlink.WorkflowLinkID, in.AgentVersionID, in.InstanceURL, in.WorkflowID, 1, tooling.JSON(vlink))
		return vlink, e
	}
	if op == "UpdateWorkflowLink" {
		var in struct {
			ExpectedRevision int64   `json:"expected_revision"`
			DisplayName      *string `json:"display_name,omitempty"`
			Enabled          *bool   `json:"enabled,omitempty"`
		}
		if tooling.StrictJSON(raw, &in) != nil || in.ExpectedRevision < 1 || (in.DisplayName == nil && in.Enabled == nil) {
			return nil, tooling.E("WORKFLOW_LINK_INVALID")
		}
		var v agenting.WorkflowLink
		if e := loadDocument(ctx, tx, "workflow_links", "workflow_link_id", id, &v); e != nil {
			return nil, e
		}
		if v.Revision != in.ExpectedRevision {
			return nil, tooling.E("REVISION_CONFLICT")
		}
		if in.DisplayName != nil {
			if !tooling.ValidLabel(*in.DisplayName, 200, true) {
				return nil, tooling.E("WORKFLOW_LINK_INVALID")
			}
			v.DisplayName = *in.DisplayName
		}
		if in.Enabled != nil {
			v.Enabled = *in.Enabled
		}
		v.Revision++
		v.UpdatedAt = tooling.Now()
		_, e := tx.Exec(ctx, `UPDATE agent.workflow_links SET revision=$2,document=$3 WHERE workflow_link_id=$1`, id, v.Revision, tooling.JSON(v))
		return v, e
	}
	return nil, tooling.E("WORKFLOW_LINK_INVALID")
}
func (s *Store) readWorkflow(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	if op == "GetWorkflowLink" {
		var v agenting.WorkflowLink
		if e := loadDocument(ctx, s.Pool, "workflow_links", "workflow_link_id", in.ResourceId, &v); e != nil {
			return nil, e
		}
		return &platformv1.ToolResponse{Json: tooling.JSON(v)}, nil
	}
	limit := in.Limit
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 {
		return nil, tooling.E("WORKFLOW_LINK_INVALID")
	}
	cursor := in.Cursor
	if cursor == "" {
		cursor = "00000000-0000-0000-0000-000000000000"
	}
	if in.Cursor != "" && !rpc.ValidID(cursor) {
		return nil, tooling.E("WORKFLOW_LINK_INVALID")
	}
	rows, e := s.Pool.Query(ctx, `SELECT w.workflow_link_id::text,w.document || jsonb_build_object(
        'agent_id',v.agent_id,'agent_display_name',a.document->>'display_name','agent_enabled',a.document->'enabled',
        'agent_version',v.version,'agent_version_state',v.state)
        FROM agent.workflow_links w JOIN agent.agent_versions v ON v.agent_version_id=w.agent_version_id
        JOIN agent.agents a ON a.agent_id=v.agent_id
        WHERE w.workflow_link_id>$1 ORDER BY w.workflow_link_id LIMIT $2`, cursor, limit+1)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	page := tooling.Page{Items: []json.RawMessage{}}
	ids := []string{}
	for rows.Next() {
		var id string
		var raw []byte
		if e = rows.Scan(&id, &raw); e != nil {
			return nil, e
		}
		ids = append(ids, id)
		page.Items = append(page.Items, raw)
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}
	if len(ids) > int(limit) {
		page.Items = page.Items[:limit]
		page.NextCursor = ids[limit-1]
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(page)}, nil
}
