package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"time"
)

type AgentRepository interface {
	ToolRepository
	AgentCommand(context.Context, *platformv1.AgentCommand) (*platformv1.ToolResponse, error)
}
type Agents struct {
	platformv1.UnimplementedAgentServiceServer
	DB      AgentRepository
	Runtime platformv1.RuntimeServiceClient
}

func (s *Agents) execute(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return (&Tools{DB: s.DB}).execute(ctx, op, in)
}
func (s *Agents) AgentRuntimeCommand(ctx context.Context, in *platformv1.AgentCommand) (*platformv1.ToolResponse, error) {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	out, err := s.DB.AgentCommand(ctx, in)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, tooling.Code(err))
	}
	return out, nil
}
func (s *Agents) InspectAgentModel(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	var req agenting.Inspection
	if tooling.StrictJSON(in.Json, &req) != nil || !rpc.ValidID(req.ConnectionVersionID) || len(req.Cursor) > 2048 || (req.Action != "deployments" && req.Action != "resolve") || (req.Action == "resolve" && (!rpc.ValidID(req.DeploymentID) || req.Cursor != "")) || (req.Action == "deployments" && req.DeploymentID != "") {
		return nil, status.Error(codes.InvalidArgument, "AGENT_INPUT_INVALID")
	}
	if s.Runtime == nil {
		return nil, status.Error(codes.Unavailable, "SERVICE_UNAVAILABLE")
	}
	ctx, cancel := context.WithTimeout(ctx, 18*time.Second)
	defer cancel()
	connection, err := s.execute(ctx, "GetConnectionVersion", &platformv1.ToolRequest{ResourceId: req.ConnectionVersionID})
	if err != nil {
		return nil, err
	}
	out, err := s.Runtime.InspectAgentModel(rpc.Outgoing(ctx, rpc.RequestID(ctx)), &platformv1.AgentModelInspection{RequestJson: tooling.JSON(req), ConnectionJson: connection.Json})
	if err != nil || out == nil {
		return nil, status.Error(codes.Unavailable, "MODEL_UNAVAILABLE")
	}
	if out.ErrorCode != "" {
		code := out.ErrorCode
		if !agenting.ErrorCode(code) && !tooling.KnowledgeError(code) {
			code = "MODEL_RESPONSE_INVALID"
		}
		return nil, status.Error(codes.FailedPrecondition, code)
	}
	var result map[string]json.RawMessage
	if tooling.NullableJSON(out.Json, &result, tooling.MaxOutput) != nil || result == nil {
		return nil, status.Error(codes.Unavailable, "MODEL_RESPONSE_INVALID")
	}
	return &platformv1.ToolResponse{Json: out.Json}, nil
}
func (s *Agents) CreateAgent(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateAgent", in)
}
func (s *Agents) ListAgents(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListAgents", in)
}
func (s *Agents) GetAgent(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetAgent", in)
}
func (s *Agents) UpdateAgent(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "UpdateAgent", in)
}
func (s *Agents) CreateAgentVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateAgentVersion", in)
}
func (s *Agents) GetAgentVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetAgentVersion", in)
}
func (s *Agents) PublishAgentVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "PublishAgentVersion", in)
}
func (s *Agents) RetireAgentVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "RetireAgentVersion", in)
}
func (s *Agents) CreateAgentRun(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateAgentRun", in)
}
func (s *Agents) GetAgentRun(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetAgentRun", in)
}
func (s *Agents) ListAgentRuns(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListAgentRuns", in)
}
func (s *Agents) GetAgentSteps(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetAgentSteps", in)
}

func (s *Agents) CreateWorkflowLink(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateWorkflowLink", in)
}
func (s *Agents) ListWorkflowLinks(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListWorkflowLinks", in)
}
func (s *Agents) GetWorkflowLink(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetWorkflowLink", in)
}
func (s *Agents) UpdateWorkflowLink(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "UpdateWorkflowLink", in)
}
