package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/tooling"
	"context"
	"google.golang.org/grpc"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

type emptyRegistry struct{ platformv1.ToolServiceClient }

func (emptyRegistry) ListMCPTools(context.Context, *platformv1.ToolRequest, ...grpc.CallOption) (*platformv1.ToolResponse, error) {
	return &platformv1.ToolResponse{Json: []byte(`{"items":[]}`)}, nil
}
func TestMCPTransportBoundaries(t *testing.T) {
	token := strings.Repeat("c", 64)
	h := (&Server{Client: emptyRegistry{}, Token: token, AllowedOrigins: []string{"http://trusted.example"}}).Handler()
	for _, tc := range []struct {
		method, auth, origin, protocol, body string
		want                                 int
	}{
		{"POST", "", "", "", `{}`, 401},
		{"POST", token, "http://evil.example", "", `{}`, 403},
		{"POST", token, "", "1900-01-01", `{}`, 400},
		{"GET", token, "", "", ``, 405},
		{"POST", token, "http://trusted.example", "", `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"test","version":"1"}}}`, 200},
		{"POST", token, "", tooling.MCPProtocol, `{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}`, 200},
	} {
		r := httptest.NewRequest(tc.method, "/mcp", strings.NewReader(tc.body))
		if tc.auth != "" {
			r.Header.Set("Authorization", "Bearer "+tc.auth)
		}
		if tc.origin != "" {
			r.Header.Set("Origin", tc.origin)
		}
		if tc.protocol != "" {
			r.Header.Set("MCP-Protocol-Version", tc.protocol)
		}
		r.Header.Set("Content-Type", "application/json")
		r.Header.Set("Accept", "application/json, text/event-stream")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("%+v: %d %s", tc, w.Code, w.Body)
		}
		if tc.want == http.StatusOK && strings.Contains(tc.body, "initialize") && !strings.Contains(w.Body.String(), tooling.MCPProtocol) {
			t.Fatal("negotiation did not pin documented protocol")
		}
	}
}
