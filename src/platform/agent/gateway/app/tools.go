package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"google.golang.org/grpc"
	"google.golang.org/grpc/status"
	"io"
	"mime"
	"net/http"
	"strconv"
)

type toolMethod func(context.Context, *platformv1.ToolRequest, ...grpc.CallOption) (*platformv1.ToolResponse, error)

func (s *Server) toolRoutes(mux *http.ServeMux) {
	if s.Tools == nil {
		return
	}
	routes := []struct {
		pattern string
		method  toolMethod
		code    int
	}{
		{"GET /api/openapi-imports", s.Tools.ListOpenAPIImports, 200}, {"GET /api/openapi-imports/{id}", s.Tools.GetOpenAPIImport, 200},
		{"POST /api/knowledge/inspect", s.Tools.InspectKnowledge, 200},
		{"POST /api/tools", s.Tools.CreateTool, 201}, {"GET /api/tools", s.Tools.ListTools, 200}, {"GET /api/tools/{id}", s.Tools.GetTool, 200}, {"PATCH /api/tools/{id}", s.Tools.UpdateTool, 200},
		{"POST /api/tools/{id}/versions", s.Tools.CreateToolVersion, 201}, {"GET /api/tool-versions/{id}", s.Tools.GetToolVersion, 200}, {"POST /api/tool-versions/{id}/publish", s.Tools.PublishToolVersion, 200}, {"POST /api/tool-versions/{id}/retire", s.Tools.RetireToolVersion, 200},
		{"POST /api/connections", s.Tools.CreateConnection, 201}, {"GET /api/connections", s.Tools.ListConnections, 200}, {"GET /api/connections/{id}", s.Tools.GetConnection, 200}, {"POST /api/connections/{id}/versions", s.Tools.CreateConnectionVersion, 201}, {"GET /api/connection-versions/{id}", s.Tools.GetConnectionVersion, 200},
		{"POST /api/tool-calls", s.Tools.CreateToolCall, 202}, {"GET /api/tool-calls/{id}", s.Tools.GetToolCall, 200},
	}
	mux.HandleFunc("POST /api/openapi-imports", s.importRequest)
	for _, route := range routes {
		mux.HandleFunc(route.pattern, func(w http.ResponseWriter, r *http.Request) { s.toolRequest(w, r, route.method, route.code) })
	}
}
func (s *Server) toolRequest(w http.ResponseWriter, r *http.Request, call toolMethod, code int) {
	in := &platformv1.ToolRequest{ResourceId: r.PathValue("id")}
	if in.ResourceId != "" && !rpc.ValidID(in.ResourceId) {
		writeError(w, 400, "TOOL_INPUT_INVALID", "invalid resource ID")
		return
	}
	if r.Method == "GET" {
		query := r.URL.Query()
		for key, values := range query {
			if (key != "limit" && key != "cursor") || len(values) != 1 || in.ResourceId != "" {
				writeError(w, 400, "TOOL_INPUT_INVALID", "invalid query")
				return
			}
		}
		if limit := query.Get("limit"); limit != "" {
			n, err := strconv.Atoi(limit)
			if err != nil || n < 1 || n > 100 {
				writeError(w, 400, "TOOL_INPUT_INVALID", "limit must be 1..100")
				return
			}
			in.Limit = int32(n)
		}
		in.Cursor = query.Get("cursor")
	} else {
		if r.URL.RawQuery != "" {
			writeError(w, 400, "TOOL_INPUT_INVALID", "query not supported")
			return
		}
		content, _, err := mime.ParseMediaType(r.Header.Get("Content-Type"))
		if err != nil || content != "application/json" {
			writeError(w, 415, "UNSUPPORTED_MEDIA_TYPE", "application/json required")
			return
		}
		if r.Method == "POST" {
			in.IdempotencyKey = r.Header.Get("Idempotency-Key")
			if len(r.Header.Values("Idempotency-Key")) != 1 || !rpc.ValidID(in.IdempotencyKey) {
				writeError(w, 400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key must be a canonical UUID")
				return
			}
		}
		body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, tooling.MaxInput))
		if err != nil {
			writeError(w, 413, "TOOL_PAYLOAD_TOO_LARGE", "request body limit exceeded")
			return
		}
		if r.URL.Path == "/api/tool-calls" {
			var v any
			err = tooling.NullableJSON(body, &v, tooling.MaxInput)
			if err == nil {
				in.Json = tooling.JSON(v)
			}
		} else {
			in.Json, err = tooling.Canonical(body)
		}
		if err != nil {
			writeError(w, 400, "TOOL_INPUT_INVALID", "invalid JSON object")
			return
		}
	}
	out, err := call(r.Context(), in)
	if err != nil {
		c := status.Convert(err).Message()
		if !knownToolCode(c) {
			c = "SERVICE_UNAVAILABLE"
		}
		writeError(w, tooling.HTTPStatus(c), c, "Tool request could not be completed")
		return
	}
	if out.Replayed {
		w.Header().Set("Idempotency-Replayed", "true")
	}
	if code == 202 {
		var accepted tooling.Accepted
		if json.Unmarshal(out.Json, &accepted) != nil {
			writeError(w, 503, "SERVICE_UNAVAILABLE", "invalid acceptance response")
			return
		}
		w.Header().Set("Location", accepted.StatusURL)
	}
	w.WriteHeader(code)
	_, _ = w.Write(out.Json)
}
func knownToolCode(s string) bool {
	if agenting.ErrorCode(s) || tooling.KnowledgeError(s) {
		return true
	}
	switch s {
	case "OPENAPI_INVALID", "OPENAPI_UNSUPPORTED", "OPENAPI_NOT_FOUND", "TOOL_TEST_REQUIRED", "TOOL_LIMIT_REACHED", "TOOL_INPUT_INVALID", "TOOL_NOT_FOUND", "CONNECTION_NOT_FOUND", "TOOL_CALL_NOT_FOUND", "REVISION_CONFLICT", "KEY_CONFLICT", "TOOL_VERSION_UNAVAILABLE", "CONNECTION_UNAVAILABLE", "TOOL_PAYLOAD_TOO_LARGE", "IDEMPOTENCY_KEY_REQUIRED":
		return true
	}
	return false
}
