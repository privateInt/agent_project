package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/apispec"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"time"
)

// Discovery performs only bounded GETs through Runtime. No database transaction
// or mutation lock is held while waiting on either external platform.
func (s *Tools) InspectKnowledge(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	var req tooling.KnowledgeInspect
	if tooling.StrictJSON(in.Json, &req) != nil || tooling.ValidateKnowledgeInspect(req) != nil {
		return nil, status.Error(codes.InvalidArgument, "TOOL_INPUT_INVALID")
	}
	if s.Runtime == nil {
		return nil, status.Error(codes.Unavailable, "SERVICE_UNAVAILABLE")
	}
	ctx, cancel := context.WithTimeout(ctx, 18*time.Second)
	defer cancel()
	connections := make([][]byte, 0, 2)
	for _, id := range []string{req.ConnectionVersionID, req.ModelConnectionVersionID} {
		c, err := s.execute(ctx, "GetConnectionVersion", &platformv1.ToolRequest{ResourceId: id})
		if err != nil {
			return nil, err
		}
		connections = append(connections, c.Json)
	}
	out, err := s.Runtime.InspectKnowledge(rpc.Outgoing(ctx, rpc.RequestID(ctx)), &platformv1.KnowledgeInspection{RequestJson: tooling.JSON(req), KnowledgeConnectionJson: connections[0], ModelConnectionJson: connections[1]})
	if err != nil {
		return nil, status.Error(codes.Unavailable, "TOOL_UPSTREAM_UNAVAILABLE")
	}
	if out == nil {
		return nil, status.Error(codes.Unavailable, "TOOL_RESPONSE_INVALID")
	}
	if out.ErrorCode != "" {
		code := out.ErrorCode
		if !tooling.KnowledgeError(code) {
			code = "TOOL_RESPONSE_INVALID"
		}
		return nil, status.Error(codes.FailedPrecondition, code)
	}
	var value map[string]any
	if tooling.NullableJSON(out.Json, &value, tooling.MaxOutput) != nil || value == nil {
		return nil, status.Error(codes.Unavailable, "TOOL_RESPONSE_INVALID")
	}
	return &platformv1.ToolResponse{Json: out.Json}, nil
}

type knowledgeEnvelope struct {
	Data       json.RawMessage  `json:"data"`
	Completion string           `json:"completion"`
	Metadata   json.RawMessage  `json:"metadata,omitempty"`
	Stage      string           `json:"stage,omitempty"`
	Timings    map[string]int64 `json:"timings_ms"`
}

func knowledgeResult(j *store.ToolJob, out *platformv1.ToolExecutionResult, err error) ([]byte, string) {
	if err != nil {
		if status.Code(err) == codes.DeadlineExceeded {
			return nil, "TOOL_TIMEOUT"
		}
		return nil, "TOOL_UPSTREAM_UNAVAILABLE"
	}
	if out == nil || out.ToolCallId != j.ID || out.ToolVersionId != j.Snapshot.Version.ToolVersionID || out.LeaseToken != j.LeaseToken || !rpc.ValidID(out.RuntimeInstanceId) {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	var b knowledgeEnvelope
	if tooling.NullableJSON(out.ResultJson, &b, tooling.MaxOutput) != nil {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	if b.Timings == nil {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	for stage, ms := range b.Timings {
		if !tooling.KnowledgeStage(stage) || ms < 0 || ms > 241000 {
			return nil, "TOOL_RESPONSE_INVALID"
		}
	}
	if out.ErrorCode != "" {
		if !tooling.KnowledgeError(out.ErrorCode) || !tooling.KnowledgeStage(b.Stage) || b.Completion != "failed" || string(b.Data) != "null" || len(b.Metadata) != 0 {
			return nil, "TOOL_RESPONSE_INVALID"
		}
		return tooling.JSON(map[string]any{"data": nil, "completion": "failed", "stage": b.Stage, "timings_ms": b.Timings, "runtime_instance_id": out.RuntimeInstanceId}), out.ErrorCode
	}
	k := j.Snapshot.Version.Binding.Knowledge
	var data struct {
		IndexID   string            `json:"index_id"`
		VersionID string            `json:"index_version_id"`
		SpaceID   string            `json:"embedding_space_id"`
		Count     int               `json:"returned_count"`
		Results   []json.RawMessage `json:"results"`
	}
	// The complete Knowledge response stays intact; this struct checks identity only.
	if k == nil || b.Completion != "completed" || b.Stage != "" || len(b.Metadata) == 0 || json.Unmarshal(b.Data, &data) != nil || data.IndexID != k.IndexID || data.VersionID != k.IndexVersionID || data.SpaceID != k.EmbeddingSpaceID || data.Results == nil || data.Count != len(data.Results) || data.Count > 50 {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	value, parseErr := apispec.JSON(b.Data, tooling.MaxOutput)
	schema, schemaErr := apispec.JSON(j.Snapshot.Version.OutputSchema, tooling.MaxInput)
	if parseErr != nil || schemaErr != nil || apispec.Validate(schema, value) != nil {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	var meta struct {
		Adapter                  string          `json:"adapter"`
		ConnectionVersionID      string          `json:"connection_version_id"`
		ModelConnectionVersionID string          `json:"model_connection_version_id"`
		IndexVersionID           string          `json:"index_version_id"`
		Profile                  json.RawMessage `json:"embedding_profile"`
		Tokens                   int             `json:"embedding_input_tokens"`
		EmbeddingCalls           int             `json:"embedding_calls"`
		SearchCalls              int             `json:"search_calls"`
	}
	var profile struct {
		DeploymentID string `json:"deployment_id"`
		SpaceID      string `json:"embedding_space_id"`
	}
	if tooling.NullableJSON(b.Metadata, &meta, tooling.MaxInput) != nil || json.Unmarshal(meta.Profile, &profile) != nil || meta.Adapter != "knowledge_search_v1" || meta.ConnectionVersionID != j.Snapshot.Version.Binding.ConnectionVersionID || meta.ModelConnectionVersionID != k.ModelConnectionVersionID || meta.IndexVersionID != k.IndexVersionID || profile.DeploymentID != k.EmbeddingDeploymentID || profile.SpaceID != k.EmbeddingSpaceID || meta.Tokens < 1 || meta.EmbeddingCalls != 1 || meta.SearchCalls != 1 {
		return nil, "TOOL_RESPONSE_INVALID"
	}
	return tooling.JSON(map[string]any{"data": b.Data, "completion": "completed", "metadata": b.Metadata, "timings_ms": b.Timings, "runtime_instance_id": out.RuntimeInstanceId}), ""
}
