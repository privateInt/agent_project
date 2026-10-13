package app_test

import (
	gateway "agent-platform/gateway/app"
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"agent-platform/internal/testdb"
	"agent-platform/internal/tooling"
	mcpapp "agent-platform/mcp/app"
	"agent-platform/service/app"
	"bytes"
	"context"
	"encoding/json"
	sdk "github.com/modelcontextprotocol/go-sdk/mcp"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"
)

type authTransport struct{ token string }

func (a authTransport) RoundTrip(r *http.Request) (*http.Response, error) {
	r = r.Clone(r.Context())
	r.Header.Set("Authorization", "Bearer "+a.token)
	return http.DefaultTransport.RoundTrip(r)
}
func as[T any](t *testing.T, raw []byte) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}
func TestToolsEndToEnd(t *testing.T) {
	address := os.Getenv("AGENT_TEST_RUNTIME_ADDRESS")
	if address == "" {
		t.Skip("requires a Python runtime; native checks profile supplies it")
	}
	db := testdb.Open(t)
	token, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		t.Fatal(err)
	}
	runtimeConn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		t.Fatal(err)
	}
	defer runtimeConn.Close()
	runtime := platformv1.NewRuntimeServiceClient(runtimeConn)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	server := grpc.NewServer(grpc.UnaryInterceptor(rpc.Authenticate(token)), grpc.MaxRecvMsgSize(3<<20))
	platformv1.RegisterToolServiceServer(server, &app.Tools{DB: db})
	platformv1.RegisterPlatformServiceServer(server, &app.Service{DB: db, Runtime: runtime})
	go server.Serve(listener)
	defer server.Stop()
	conn, err := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	tools := platformv1.NewToolServiceClient(conn)
	mcpHTTP := httptest.NewServer((&mcpapp.Server{Client: tools, Token: token}).Handler())
	defer mcpHTTP.Close()
	target, _ := url.Parse(mcpHTTP.URL)
	rest := httptest.NewServer((&gateway.Server{Client: platformv1.NewPlatformServiceClient(conn), Tools: tools, Token: token, MCP: httputil.NewSingleHostReverseProxy(target)}).Handler())
	defer rest.Close()
	request := func(method, path, key string, body any, expected int) ([]byte, http.Header) {
		t.Helper()
		var raw []byte
		if body != nil {
			raw = tooling.JSON(body)
		}
		req, _ := http.NewRequest(method, rest.URL+path, bytes.NewReader(raw))
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Request-ID", rpc.NewID())
		if key != "" {
			req.Header.Set("Idempotency-Key", key)
		}
		res, e := http.DefaultClient.Do(req)
		if e != nil {
			t.Fatal(e)
		}
		defer res.Body.Close()
		b, _ := io.ReadAll(res.Body)
		if res.StatusCode != expected {
			t.Fatalf("%s %s: %d %s", method, path, res.StatusCode, b)
		}
		return b, res.Header
	}
	create := func(name string) tooling.Tool {
		b, _ := request("POST", "/api/tools", rpc.NewID(), tooling.ToolCreate{DisplayName: name}, 201)
		tool := as[tooling.Tool](t, b)
		testdb.DisableOnCleanup(t, db, tool.ToolID)
		return tool
	}
	version := func(tool tooling.Tool, kind string, binding tooling.Binding, timeout int) tooling.ToolVersion {
		b, _ := request("POST", "/api/tools/"+tool.ToolID+"/versions", rpc.NewID(), tooling.VersionCreate{ExpectedRevision: tool.Revision, Kind: kind, Binding: binding, TimeoutMS: timeout}, 201)
		return as[tooling.ToolVersion](t, b)
	}
	publish := func(v tooling.ToolVersion) tooling.ToolVersion {
		b, _ := request("POST", "/api/tool-versions/"+v.ToolVersionID+"/publish", rpc.NewID(), tooling.Revision{ExpectedRevision: v.Revision}, 200)
		return as[tooling.ToolVersion](t, b)
	}
	wait := func(id string) tooling.Call {
		deadline := time.Now().Add(10 * time.Second)
		for time.Now().Before(deadline) {
			b, _ := request("GET", "/api/tool-calls/"+id, "", nil, 200)
			c := as[tooling.Call](t, b)
			if c.State != "accepted" && c.State != "running" {
				return c
			}
			time.Sleep(40 * time.Millisecond)
		}
		t.Fatal("Tool did not finish")
		return tooling.Call{}
	}
	workerCtx, stop := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); (&app.ToolWorker{DB: db, Runtime: runtime}).Run(workerCtx) }()
	defer func() { stop(); <-done }()
	tool := create("A03 REST/MCP integration")
	v := version(tool, "diagnostic_echo_v1", tooling.Binding{Prefix: "version-one:", DelayMS: 200}, 30000)
	body := tooling.CallCreate{ToolVersionID: v.ToolVersionID, Arguments: []byte(`{"text":"hello"}`), Mode: "run"}
	request("POST", "/api/tool-calls", rpc.NewID(), body, 409)
	body.Mode = "test"
	b, _ := request("POST", "/api/tool-calls", rpc.NewID(), body, 202)
	draft := wait(as[tooling.Accepted](t, b).ToolCallID)
	if draft.State != "succeeded" {
		t.Fatalf("draft test: %+v", draft)
	}
	v = publish(v)
	body.Mode = "run"
	ctx, cancel := context.WithTimeout(context.Background(), 40*time.Second)
	defer cancel()
	session, err := sdk.NewClient(&sdk.Implementation{Name: "a03-test", Version: "1"}, nil).Connect(ctx, &sdk.StreamableClientTransport{Endpoint: rest.URL + "/mcp", HTTPClient: &http.Client{Transport: authTransport{token}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	name := "t_" + strings.ReplaceAll(tool.ToolID, "-", "") + "_v1"
	listed := func() bool {
		cursor := ""
		for {
			out, e := session.ListTools(ctx, &sdk.ListToolsParams{Cursor: cursor})
			if e != nil {
				t.Fatal(e)
			}
			for _, entry := range out.Tools {
				if entry.Name == name {
					return true
				}
			}
			if out.NextCursor == "" {
				return false
			}
			cursor = out.NextCursor
		}
	}
	if !listed() {
		t.Fatal("published Tool absent from MCP")
	}
	key := rpc.NewID()
	b, _ = request("POST", "/api/tool-calls", key, body, 202)
	accepted := as[tooling.Accepted](t, b)
	// Pin a second definition while the first accepted call is still executing.
	tool.Revision = 2
	v2 := publish(version(tool, "diagnostic_echo_v1", tooling.Binding{Prefix: "version-two:"}, 30000))
	_ = v2
	enabled := false
	request("PATCH", "/api/tools/"+tool.ToolID, "", tooling.ToolPatch{ExpectedRevision: 3, Enabled: &enabled}, 200)
	request("POST", "/api/tool-calls", rpc.NewID(), body, 409)
	if listed() {
		t.Fatal("disabled Tool visible")
	}
	result, err := session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": key, "arguments": map[string]string{"text": "hello"}}})
	if err != nil || result.IsError {
		t.Fatalf("MCP replay: %v %v", result, err)
	}
	c := as[tooling.Call](t, tooling.JSON(result.StructuredContent))
	if c.ToolCallID != accepted.ToolCallID || c.ToolVersionID != v.ToolVersionID || !bytes.Contains(c.Result, []byte("version-one:hello")) {
		t.Fatalf("REST/MCP identity or snapshot changed: %+v", c)
	}
	b, headers := request("POST", "/api/tool-calls", key, body, 202)
	if headers.Get("Idempotency-Replayed") != "true" || as[tooling.Accepted](t, b).ToolCallID != c.ToolCallID {
		t.Fatal("REST replay changed")
	}
	result, err = session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": key, "arguments": map[string]string{"text": "changed"}}})
	if err != nil || !result.IsError || !bytes.Contains(tooling.JSON(result.StructuredContent), []byte("KEY_CONFLICT")) {
		t.Fatalf("key conflict %v %v", result, err)
	}
	result, err = session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": rpc.NewID(), "arguments": map[string]string{"text": "new"}}})
	if err != nil || !result.IsError {
		t.Fatal("disabled new MCP call accepted")
	}
	request("POST", "/api/tool-versions/"+v.ToolVersionID+"/retire", rpc.NewID(), tooling.Revision{ExpectedRevision: 2}, 200)
	result, err = session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": key, "arguments": map[string]string{"text": "hello"}}})
	if err != nil || result.IsError {
		t.Fatal("retired replay unavailable")
	}
	for _, tc := range []struct {
		name    string
		binding tooling.Binding
		timeout int
		code    string
	}{{"failure", tooling.Binding{Fail: true}, 30000, "TOOL_DIAGNOSTIC_FAILURE"}, {"timeout", tooling.Binding{DelayMS: 2000}, 300, "TOOL_TIMEOUT"}} {
		tv := publish(version(create(tc.name), "diagnostic_echo_v1", tc.binding, tc.timeout))
		b, _ = request("POST", "/api/tool-calls", rpc.NewID(), tooling.CallCreate{ToolVersionID: tv.ToolVersionID, Arguments: []byte(`{"text":"check"}`), Mode: "run"}, 202)
		failed := wait(as[tooling.Accepted](t, b).ToolCallID)
		if failed.State != "failed" || failed.Error == nil || failed.Error.Code != tc.code {
			t.Fatalf("%s: %+v", tc.name, failed)
		}
	}
	ref := "missing_" + strings.ReplaceAll(rpc.NewID(), "-", "")
	b, _ = request("POST", "/api/connections", rpc.NewID(), tooling.ConnectionCreate{DisplayName: "missing credential test", ConnectionSpec: tooling.ConnectionSpec{BaseURL: "http://uncontacted-api:8000", AuthType: "bearer", CredentialRef: ref, CredentialRevision: "v1"}}, 201)
	connection := as[tooling.Connection](t, b)
	cv := publish(version(create("credential reference check"), "diagnostic_connection_v1", tooling.Binding{ConnectionVersionID: connection.CurrentVersionID}, 30000))
	request("POST", "/api/connections/"+connection.ConnectionID+"/versions", rpc.NewID(), tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "http://uncontacted-api:8000", AuthType: "none"}}, 201)
	b, _ = request("POST", "/api/tool-calls", rpc.NewID(), tooling.CallCreate{ToolVersionID: cv.ToolVersionID, Arguments: []byte(`{}`), Mode: "run"}, 202)
	missing := wait(as[tooling.Accepted](t, b).ToolCallID)
	if missing.Error == nil || missing.Error.Code != "CREDENTIAL_NOT_CONFIGURED" {
		t.Fatalf("immutable credential revision: %+v", missing)
	}
	req, _ := http.NewRequest("POST", rest.URL+"/mcp", strings.NewReader(`{}`))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Origin", "https://untrusted.example")
	res, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	res.Body.Close()
	if res.StatusCode != 403 {
		t.Fatal("Origin accepted")
	}
	if script := os.Getenv("AGENT_TEST_SMOKE_SCRIPT"); script != "" {
		cmd := exec.Command("python3", script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"))
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("deployment smoke: %v %s", err, output)
		}
		var summary struct {
			Result     string `json:"result"`
			ToolCallID string `json:"tool_call_id"`
		}
		if json.Unmarshal(output, &summary) != nil || summary.Result != "passed" {
			t.Fatalf("smoke result %s", output)
		}
		follow := exec.Command("python3", script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"), "--call-id", summary.ToolCallID)
		lookup, err := follow.CombinedOutput()
		if err != nil {
			t.Fatalf("smoke lookup: %v %s", err, lookup)
		}
		t.Log("delivered Python REST/MCP smoke and read-only lookup passed")
	}
	t.Logf("REST/MCP shared call retained: %s; Python runtime, SQL, schema, snapshots, errors, authentication and Origin verified", c.ToolCallID)
}
