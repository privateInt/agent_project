package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/tooling"
	"context"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type ToolRepository interface {
	ToolAPI(context.Context, string, *platformv1.ToolRequest) (*platformv1.ToolResponse, error)
}
type Tools struct {
	platformv1.UnimplementedToolServiceServer
	DB      ToolRepository
	Runtime platformv1.RuntimeServiceClient
}

func (s *Tools) execute(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	out, err := s.DB.ToolAPI(ctx, op, in)
	if err == nil {
		return out, nil
	}
	code := tooling.Code(err)
	grpcCode := codes.Unavailable
	switch tooling.HTTPStatus(code) {
	case 400:
		grpcCode = codes.InvalidArgument
	case 404:
		grpcCode = codes.NotFound
	case 409:
		grpcCode = codes.FailedPrecondition
	case 413:
		grpcCode = codes.ResourceExhausted
	case 504:
		grpcCode = codes.DeadlineExceeded
	}
	return nil, status.Error(grpcCode, code)
}
func (s *Tools) CreateTool(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateTool", in)
}
func (s *Tools) ListTools(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListTools", in)
}
func (s *Tools) GetTool(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetTool", in)
}
func (s *Tools) UpdateTool(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "UpdateTool", in)
}
func (s *Tools) CreateToolVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateToolVersion", in)
}
func (s *Tools) GetToolVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetToolVersion", in)
}
func (s *Tools) PublishToolVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "PublishToolVersion", in)
}
func (s *Tools) RetireToolVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "RetireToolVersion", in)
}
func (s *Tools) CreateConnection(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateConnection", in)
}
func (s *Tools) ListConnections(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListConnections", in)
}
func (s *Tools) GetConnection(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetConnection", in)
}
func (s *Tools) CreateConnectionVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateConnectionVersion", in)
}
func (s *Tools) GetConnectionVersion(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetConnectionVersion", in)
}
func (s *Tools) CreateToolCall(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateToolCall", in)
}
func (s *Tools) GetToolCall(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetToolCall", in)
}
func (s *Tools) ListMCPTools(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListMCPTools", in)
}

func (s *Tools) CreateOpenAPIImport(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "CreateOpenAPIImport", in)
}
func (s *Tools) ListOpenAPIImports(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "ListOpenAPIImports", in)
}
func (s *Tools) GetOpenAPIImport(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	return s.execute(ctx, "GetOpenAPIImport", in)
}
