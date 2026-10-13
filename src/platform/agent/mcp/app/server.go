// Package app exposes published Agent Tools with the official MCP Go SDK.
package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"agent-platform/internal/version"
	"context"
	"encoding/json"
	"errors"
	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc/status"
	"net/http"
	"strings"
	"time"
)

type Server struct {
	Client         platformv1.ToolServiceClient
	Token          string
	AllowedOrigins []string
}
type entriesKey struct{}

func (s *Server) Handler() http.Handler {
	transport := sdk.NewStreamableHTTPHandler(func(r *http.Request) *sdk.Server {
		return s.sdkServer(r.Context().Value(entriesKey{}).([]tooling.MCPEntry))
	}, &sdk.StreamableHTTPOptions{Stateless: true, JSONResponse: true})
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if r.URL.Path == "/healthz" && r.Method == "GET" {
			w.WriteHeader(200)
			_, _ = w.Write([]byte("ok"))
			return
		}
		if r.URL.Path != "/mcp" || r.URL.RawQuery != "" {
			http.Error(w, "not found", 404)
			return
		}
		id := r.Header.Get("X-Request-ID")
		if id == "" {
			id = rpc.NewID()
		}
		if !rpc.ValidID(id) || len(r.Header.Values("X-Request-ID")) > 1 {
			http.Error(w, "invalid request ID", 400)
			return
		}
		w.Header().Set("X-Request-ID", id)
		if len(r.Header.Values("Authorization")) != 1 || !rpc.Authorized(r.Header.Get("Authorization"), s.Token) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			http.Error(w, "authentication required", 401)
			return
		}
		origins := r.Header.Values("Origin")
		if len(origins) > 0 {
			allowed := false
			if len(origins) == 1 {
				for _, origin := range s.AllowedOrigins {
					if origin != "" && origins[0] == origin {
						allowed = true
					}
				}
			}
			if !allowed {
				http.Error(w, "origin not allowed", 403)
				return
			}
		}
		// The SDK owns protocol rejection and negotiation, including downgrade
		// from newer clients to our configured 2025-11-25 protocol.
		ctx, cancel := context.WithTimeout(r.Context(), 35*time.Second)
		defer cancel()
		ctx = rpc.Outgoing(ctx, id)
		listctx, stop := context.WithTimeout(ctx, 3*time.Second)
		entries, err := s.entries(listctx)
		stop()
		if err != nil {
			http.Error(w, "Agent Tool registry unavailable", 503)
			return
		}
		r = r.WithContext(context.WithValue(ctx, entriesKey{}, entries))
		r.Body = http.MaxBytesReader(w, r.Body, tooling.MaxInput)
		// Override a parent JSON default: the SDK owns protocol response headers.
		w.Header().Del("Content-Type")
		transport.ServeHTTP(w, r)
	})
}
func (s *Server) entries(ctx context.Context) ([]tooling.MCPEntry, error) {
	var all []tooling.MCPEntry
	cursor := ""
	for page := 0; page < 50; page++ {
		out, err := s.Client.ListMCPTools(ctx, &platformv1.ToolRequest{Limit: 20, Cursor: cursor})
		if err != nil {
			return nil, err
		}
		var p tooling.Page
		if json.Unmarshal(out.Json, &p) != nil {
			return nil, errors.New("invalid registry")
		}
		for _, raw := range p.Items {
			var entry tooling.MCPEntry
			if json.Unmarshal(raw, &entry) != nil {
				return nil, errors.New("invalid Tool")
			}
			all = append(all, entry)
		}
		if p.NextCursor == "" {
			return all, nil
		}
		cursor = p.NextCursor
	}
	return nil, errors.New("A03 MCP registry exceeds 1000 versions")
}
func (s *Server) sdkServer(entries []tooling.MCPEntry) *sdk.Server {
	server := sdk.NewServer(&sdk.Implementation{Name: "agent-platform", Version: version.Build}, &sdk.ServerOptions{Capabilities: &sdk.ServerCapabilities{Tools: &sdk.ToolCapabilities{}}, SupportedProtocolVersions: []string{tooling.MCPProtocol}, PageSize: 100})
	available := map[string]bool{}
	for _, entry := range entries {
		e := entry
		available[e.Name] = e.Available
		schema := map[string]any{"type": "object", "properties": map[string]any{"invocation_key": map[string]any{"type": "string", "format": "uuid"}, "arguments": e.Version.InputSchema}, "required": []string{"invocation_key", "arguments"}, "additionalProperties": false}
		server.AddTool(&sdk.Tool{Name: e.Name, Title: e.DisplayName, Description: e.Description, InputSchema: schema}, func(ctx context.Context, req *sdk.CallToolRequest) (*sdk.CallToolResult, error) {
			return s.call(ctx, e.Version.ToolVersionID, req.Params.Arguments)
		})
	}
	// Retired/disabled versions remain addressable for idempotent replay, but
	// never appear in tools/list. The service rejects all new calls to them.
	server.AddReceivingMiddleware(func(next sdk.MethodHandler) sdk.MethodHandler {
		return func(ctx context.Context, method string, req sdk.Request) (sdk.Result, error) {
			result, err := next(ctx, method, req)
			if err == nil && method == "tools/list" {
				p := result.(*sdk.ListToolsResult)
				filtered := p.Tools[:0]
				for _, tool := range p.Tools {
					if available[tool.Name] {
						filtered = append(filtered, tool)
					}
				}
				p.Tools = filtered
			}
			return result, err
		}
	})
	return server
}
func toolResult(value any, isError bool) (*sdk.CallToolResult, error) {
	raw := tooling.JSON(value)
	return &sdk.CallToolResult{Content: []sdk.Content{&sdk.TextContent{Text: string(raw)}}, StructuredContent: value, IsError: isError}, nil
}
func (s *Server) call(ctx context.Context, versionID string, raw []byte) (*sdk.CallToolResult, error) {
	var in struct {
		InvocationKey string          `json:"invocation_key"`
		Arguments     json.RawMessage `json:"arguments"`
	}
	if tooling.NullableJSON(raw, &in, tooling.MaxInput) != nil || !rpc.ValidID(in.InvocationKey) || len(in.Arguments) == 0 {
		return toolResult(map[string]any{"error": map[string]string{"code": "TOOL_INPUT_INVALID"}}, true)
	}
	body := tooling.JSON(tooling.CallCreate{ToolVersionID: versionID, Arguments: in.Arguments, Mode: "run"})
	out, err := s.Client.CreateToolCall(ctx, &platformv1.ToolRequest{IdempotencyKey: in.InvocationKey, Json: body})
	if err != nil {
		code := status.Convert(err).Message()
		if !strings.HasPrefix(code, "TOOL_") && code != "KEY_CONFLICT" {
			code = "SERVICE_UNAVAILABLE"
		}
		return toolResult(map[string]any{"error": map[string]string{"code": code}}, true)
	}
	var accepted tooling.Accepted
	if json.Unmarshal(out.Json, &accepted) != nil {
		return toolResult(map[string]any{"error": map[string]string{"code": "SERVICE_UNAVAILABLE"}}, true)
	}
	for {
		result, err := s.Client.GetToolCall(ctx, &platformv1.ToolRequest{ResourceId: accepted.ToolCallID})
		if err == nil {
			var c tooling.Call
			if json.Unmarshal(result.Json, &c) == nil && c.State != "accepted" && c.State != "running" {
				return toolResult(c, c.State != "succeeded")
			}
		}
		if err != nil || ctx.Err() != nil {
			return toolResult(map[string]any{"tool_call_id": accepted.ToolCallID, "tool_version_id": versionID, "status_url": accepted.StatusURL, "error": map[string]string{"code": "TOOL_WAIT_TIMEOUT"}}, true)
		}
		timer := time.NewTimer(100 * time.Millisecond)
		select {
		case <-ctx.Done():
			timer.Stop()
			return toolResult(map[string]any{"tool_call_id": accepted.ToolCallID, "status_url": accepted.StatusURL, "error": map[string]string{"code": "TOOL_WAIT_TIMEOUT"}}, true)
		case <-timer.C:
		}
	}
}
