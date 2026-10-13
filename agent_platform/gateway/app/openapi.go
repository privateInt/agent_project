package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/apispec"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"google.golang.org/grpc/status"
	"io"
	"mime"
	"net/http"
)

func (s *Server) importRequest(w http.ResponseWriter, r *http.Request) {
	if r.URL.RawQuery != "" {
		writeError(w, 400, "TOOL_INPUT_INVALID", "query unsupported")
		return
	}
	key := r.Header.Get("Idempotency-Key")
	if len(r.Header.Values("Idempotency-Key")) != 1 || !rpc.ValidID(key) {
		writeError(w, 400, "IDEMPOTENCY_KEY_REQUIRED", "Idempotency-Key must be a canonical UUID")
		return
	}
	typ, _, e := mime.ParseMediaType(r.Header.Get("Content-Type"))
	format := ""
	if e == nil {
		switch typ {
		case "application/json":
			format = "json"
		case "application/yaml", "application/x-yaml", "text/yaml":
			format = "yaml"
		}
	}
	if format == "" {
		writeError(w, 415, "UNSUPPORTED_MEDIA_TYPE", "upload UTF-8 JSON or YAML as the request body")
		return
	}
	raw, e := io.ReadAll(http.MaxBytesReader(w, r.Body, apispec.MaxDocument))
	if e != nil {
		writeError(w, 413, "TOOL_PAYLOAD_TOO_LARGE", "OpenAPI file limit is 5 MiB")
		return
	}
	out, e := s.Tools.CreateOpenAPIImport(r.Context(), &platformv1.ToolRequest{IdempotencyKey: key, Json: raw, DocumentFormat: format})
	if e != nil {
		code := status.Convert(e).Message()
		if !knownToolCode(code) {
			code = "SERVICE_UNAVAILABLE"
		}
		writeError(w, tooling.HTTPStatus(code), code, "could not parse/store the OpenAPI document; check format, version and size")
		return
	}
	if out.Replayed {
		w.Header().Set("Idempotency-Replayed", "true")
	}
	w.WriteHeader(201)
	_, _ = w.Write(out.Json)
}
