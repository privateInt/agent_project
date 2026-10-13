package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/version"
	"context"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"io"
	"log/slog"
	"net/http"
	"time"
)

type Server struct {
	Client   platformv1.PlatformServiceClient
	Token    string
	Tools    platformv1.ToolServiceClient
	Agents   platformv1.AgentServiceClient
	MCP      http.Handler
	MCPReady func(context.Context) bool
}

func (s *Server) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		id := r.Header.Get("X-Request-ID")
		if id == "" {
			id = rpc.NewID()
		}
		w.Header().Set("Content-Type", "application/json; charset=utf-8")
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		if !rpc.ValidID(id) || len(r.Header.Values("X-Request-ID")) > 1 {
			writeError(w, 400, "INVALID_REQUEST_ID", "X-Request-ID must be a canonical UUID")
			return
		}
		w.Header().Set("X-Request-ID", id)
		defer func() {
			slog.Info("http_request", "request_id", id, "method", r.Method, "elapsed_ms", time.Since(started).Milliseconds())
		}()
		timeout := 5 * time.Second
		if r.URL.Path == "/api/knowledge/inspect" || r.URL.Path == "/api/agent-models/inspect" {
			timeout = 19 * time.Second
		}
		if r.URL.Path == "/mcp" {
			timeout = 40 * time.Second
		}
		ctx, cancel := context.WithTimeout(r.Context(), timeout)
		defer cancel()
		r = r.WithContext(rpc.Outgoing(ctx, id))
		if console(w, r) {
			return
		}
		if r.URL.Path == "/healthz" {
			if r.Method != "GET" {
				method(w, "GET")
				return
			}
			writeJSON(w, 200, map[string]string{"status": "ok", "platform": "agent"})
			return
		}
		if r.URL.Path == "/readyz" {
			if r.Method != "GET" {
				method(w, "GET")
				return
			}
			s.ready(w, r)
			return
		}
		if r.URL.Path == "/" {
			if r.Method != "GET" {
				method(w, "GET")
				return
			}
			writeJSON(w, 200, map[string]string{"platform": "agent", "stage": version.Stage, "status_api": "/api/status"})
			return
		}
		if len(r.Header.Values("Authorization")) != 1 || !rpc.Authorized(r.Header.Get("Authorization"), s.Token) {
			w.Header().Set("WWW-Authenticate", "Bearer")
			writeError(w, 401, "UNAUTHENTICATED", "authentication required")
			return
		}
		if r.URL.Path == "/mcp" && s.MCP != nil {
			r.Header.Set("X-Request-ID", id)
			s.MCP.ServeHTTP(w, r)
			return
		}
		mux := http.NewServeMux()
		s.toolRoutes(mux)
		s.agentRoutes(mux)
		mux.HandleFunc("GET /api/status", s.status)
		mux.HandleFunc("POST /api/system-checks", s.create)
		mux.HandleFunc("GET /api/system-checks/{id}", s.get)
		mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) { writeError(w, 404, "NOT_FOUND", "endpoint not found") })
		mux.ServeHTTP(w, r)
	})
}
func method(w http.ResponseWriter, allowed string) {
	w.Header().Set("Allow", allowed)
	writeError(w, 405, "METHOD_NOT_ALLOWED", "method not allowed")
}
func writeJSON(w http.ResponseWriter, code int, v any) {
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
func writeError(w http.ResponseWriter, code int, name, message string) {
	writeJSON(w, code, map[string]any{"error": map[string]string{"code": name, "message": message}})
}
func writeProto(w http.ResponseWriter, code int, v proto.Message) {
	data, err := (protojson.MarshalOptions{UseProtoNames: true, EmitUnpopulated: true}).Marshal(v)
	if err != nil {
		writeError(w, 500, "INTERNAL", "response encoding failed")
		return
	}
	w.WriteHeader(code)
	_, _ = w.Write(data)
}
func writeRPCError(w http.ResponseWriter, err error) {
	switch status.Code(err) {
	case codes.InvalidArgument:
		writeError(w, 400, "INVALID_ARGUMENT", "invalid request")
	case codes.NotFound:
		writeError(w, 404, "NOT_FOUND", "system check not found")
	case codes.DeadlineExceeded:
		writeError(w, 504, "DEADLINE_EXCEEDED", "service timeout")
	default:
		writeError(w, 503, "SERVICE_UNAVAILABLE", "Agent service unavailable")
	}
}
func (s *Server) ready(w http.ResponseWriter, r *http.Request) {
	out, err := s.Client.GetStatus(r.Context(), &emptypb.Empty{})
	if err != nil || !out.GetReady() || s.MCPReady != nil && !s.MCPReady(r.Context()) {
		writeError(w, 503, "NOT_READY", "Agent is not ready")
		return
	}
	writeJSON(w, 200, map[string]string{"status": "ready"})
}
func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	out, err := s.Client.GetStatus(r.Context(), &emptypb.Empty{})
	if err != nil {
		writeRPCError(w, err)
		return
	}
	code := 200
	if !out.Ready {
		code = 503
	}
	writeProto(w, code, out)
}
func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	body, err := io.ReadAll(http.MaxBytesReader(w, r.Body, 1024))
	if err != nil {
		writeError(w, 413, "BODY_TOO_LARGE", "body limit is 1024 bytes")
		return
	}
	if len(body) > 0 {
		var object map[string]json.RawMessage
		if err = json.Unmarshal(body, &object); err != nil || object == nil || len(object) != 0 {
			writeError(w, 400, "INVALID_ARGUMENT", "body must be empty or {}")
			return
		}
	}
	out, err := s.Client.CreateSystemCheck(r.Context(), &emptypb.Empty{})
	if err != nil {
		writeRPCError(w, err)
		return
	}
	w.Header().Set("Location", "/api/system-checks/"+out.CheckId)
	writeProto(w, 201, out)
}
func (s *Server) get(w http.ResponseWriter, r *http.Request) {
	id := r.PathValue("id")
	if !rpc.ValidID(id) {
		writeError(w, 400, "INVALID_ARGUMENT", "invalid check_id")
		return
	}
	out, err := s.Client.GetSystemCheck(r.Context(), &platformv1.GetSystemCheckRequest{CheckId: id})
	if err != nil {
		writeRPCError(w, err)
		return
	}
	writeProto(w, 200, out)
}
