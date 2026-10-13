package app_test

import (
	gateway "agent-platform/gateway/app"
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/testdb"
	"agent-platform/internal/tooling"
	mcpapp "agent-platform/mcp/app"
	"agent-platform/service/app"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
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
	"sync/atomic"
	"testing"
	"time"
)

func TestOpenAPIEndToEnd(t *testing.T) {
	address := os.Getenv("AGENT_TEST_RUNTIME_ADDRESS")
	if address == "" {
		t.Skip("requires Python runtime and SQL")
	}
	db := testdb.Open(t)
	token, e := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if e != nil {
		t.Fatal(e)
	}
	runtimeConn, e := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(8<<20)))
	if e != nil {
		t.Fatal(e)
	}
	defer runtimeConn.Close()
	runtime := platformv1.NewRuntimeServiceClient(runtimeConn)
	listener, e := net.Listen("tcp", "127.0.0.1:0")
	if e != nil {
		t.Fatal(e)
	}
	service := grpc.NewServer(grpc.UnaryInterceptor(rpc.Authenticate(token)), grpc.MaxRecvMsgSize(8<<20), grpc.MaxSendMsgSize(8<<20))
	platformv1.RegisterToolServiceServer(service, &app.Tools{DB: db})
	platformv1.RegisterPlatformServiceServer(service, &app.Service{DB: db, Runtime: runtime})
	go service.Serve(listener)
	defer service.Stop()
	conn, e := grpc.NewClient(listener.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(8<<20), grpc.MaxCallSendMsgSize(8<<20)))
	if e != nil {
		t.Fatal(e)
	}
	defer conn.Close()
	tools := platformv1.NewToolServiceClient(conn)
	mcp := httptest.NewServer((&mcpapp.Server{Client: tools, Token: token}).Handler())
	defer mcp.Close()
	target, _ := url.Parse(mcp.URL)
	rest := httptest.NewServer((&gateway.Server{Client: platformv1.NewPlatformServiceClient(conn), Tools: tools, Token: token, MCP: httputil.NewSingleHostReverseProxy(target)}).Handler())
	defer rest.Close()
	request := func(method, path, key string, body []byte, expected int) []byte {
		t.Helper()
		req, _ := http.NewRequest(method, rest.URL+path, bytes.NewReader(body))
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Content-Type", "application/json")
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
			t.Fatalf("%s %s = %d: %s", method, path, res.StatusCode, b)
		}
		return b
	}
	workerCtx, stop := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); (&app.ToolWorker{DB: db, Runtime: runtime}).Run(workerCtx) }()
	defer func() { stop(); <-done }()
	// This fixture is reachable by the separate Python container on agent-private.
	// It uses a real non-loopback address; production SSRF checks stay enabled.
	host := os.Getenv("AGENT_TEST_HTTP_HOST")
	if host == "" {
		addresses, _ := net.InterfaceAddrs()
		for _, a := range addresses {
			ip, _, _ := net.ParseCIDR(a.String())
			if ip != nil && ip.To4() != nil && !ip.IsLoopback() && !ip.IsLinkLocalUnicast() {
				host = ip.String()
				break
			}
		}
	}
	if host == "" {
		t.Fatal("no non-loopback interface for HTTP fixture")
	}
	var hits atomic.Int64
	fixture := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		hits.Add(1)
		w.Header().Set("Content-Type", "application/json")
		if r.URL.Path == "/v1/async" {
			w.Header().Set("Location", "/jobs/example?private=hidden")
			w.WriteHeader(202)
			_, _ = w.Write([]byte(`{}`))
			return
		}
		if r.URL.Path == "/v1/slow" {
			time.Sleep(1500 * time.Millisecond)
		}
		if r.URL.Path == "/v1/bad" {
			_, _ = w.Write([]byte(`{"unexpected":true}`))
			return
		}
		if r.URL.Path == "/v1/empty" {
			w.WriteHeader(204)
			return
		}
		if r.URL.Path == "/v1/redirect" {
			w.Header().Set("Location", "http://127.0.0.1/private")
			w.WriteHeader(302)
			return
		}
		if r.URL.Path == "/v1/large" {
			_, _ = w.Write([]byte(`{"value":null,"padding":"` + strings.Repeat("x", 300000) + `"}`))
			return
		}
		var body any
		if r.Body != nil {
			_ = json.NewDecoder(r.Body).Decode(&body)
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"value": nil, "target": r.URL.RequestURI(), "header": r.Header.Get("X-Label"), "key": r.Header.Get("Idempotency-Key"), "body": body})
	}))
	l, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		t.Fatal(e)
	}
	fixture.Listener = l
	fixture.Start()
	defer fixture.Close()
	baseURL := "http://" + net.JoinHostPort(host, fmt.Sprint(l.Addr().(*net.TCPAddr).Port)) + "/v1"
	cvBody := request("POST", "/api/connections", rpc.NewID(), tooling.JSON(tooling.ConnectionCreate{DisplayName: "A04 test endpoint", ConnectionSpec: tooling.ConnectionSpec{BaseURL: baseURL, AuthType: "none"}}), 201)
	cv := as[tooling.Connection](t, cvBody)
	spec := []byte(`{"openapi":"3.1.1","info":{"title":"A04 HTTP integration","version":"1"},"paths":{"/items/{id}":{"post":{"operationId":"sameName","parameters":[{"name":"id","in":"path","required":true,"schema":{"type":"string"}},{"name":"id","in":"query","schema":{"type":"array","items":{"type":"string"}}},{"name":"X-Label","in":"header","schema":{"type":"string"}}],"requestBody":{"required":true,"content":{"application/json":{"schema":{"type":"object","properties":{"id":{"type":["string","null"]}},"required":["id"],"additionalProperties":false}}}},"responses":{"200":{"description":"ok","content":{"application/json":{"schema":{"type":"object","properties":{"value":{"type":["string","null"]}},"required":["value"]}}}}}}},"/async":{"post":{"responses":{"200":{"description":"ok","content":{"application/json":{"schema":{"type":"object","properties":{"value":{"type":["string","null"]}},"required":["value"]}}}}}}}},"components":{}}`)
	var document map[string]any
	if json.Unmarshal(spec, &document) != nil {
		t.Fatal("invalid fixture")
	}
	paths := document["paths"].(map[string]any)
	response := map[string]any{"200": map[string]any{"description": "ok", "content": map[string]any{"application/json": map[string]any{"schema": map[string]any{"type": "object", "properties": map[string]any{"value": map[string]any{"type": []any{"string", "null"}}}, "required": []string{"value"}}}}}}
	for _, path := range []string{"/slow", "/bad", "/redirect", "/large"} {
		paths[path] = map[string]any{"post": map[string]any{"responses": response}, "get": map[string]any{"responses": response}}
	}
	paths["/empty"] = map[string]any{"get": map[string]any{"responses": map[string]any{"204": map[string]any{"description": "done"}}}}
	paths["/unsupported"] = map[string]any{"get": map[string]any{"responses": map[string]any{"202": map[string]any{"description": "later"}}}}
	spec = tooling.JSON(document)
	importKey := rpc.NewID()
	imported := as[store.OpenAPIImport](t, request("POST", "/api/openapi-imports", importKey, spec, 201))
	replay := as[store.OpenAPIImport](t, request("POST", "/api/openapi-imports", importKey, spec, 201))
	if replay.ImportID != imported.ImportID {
		t.Fatal("import replay changed")
	}
	document["info"].(map[string]any)["version"] = "2"
	request("POST", "/api/openapi-imports", importKey, tooling.JSON(document), 409)
	if hits.Load() != 0 {
		t.Fatal("import made HTTP requests")
	}
	request("GET", "/api/openapi-imports/"+imported.ImportID, "", nil, 200)
	request("GET", "/api/openapi-imports?limit=1", "", nil, 200)
	makeVersion := func(operation, effect string, timeout int) tooling.ToolVersion {
		t.Helper()
		tool := as[tooling.Tool](t, request("POST", "/api/tools", rpc.NewID(), tooling.JSON(tooling.ToolCreate{DisplayName: "sameName"}), 201))
		testdb.DisableOnCleanup(t, db, tool.ToolID)
		return as[tooling.ToolVersion](t, request("POST", "/api/tools/"+tool.ToolID+"/versions", rpc.NewID(), tooling.JSON(tooling.VersionCreate{ExpectedRevision: 1, Kind: "openapi_http_v1", Binding: tooling.Binding{ImportID: imported.ImportID, OperationKey: operation, AuthScheme: "none", ConnectionVersionID: cv.CurrentVersionID, Effect: effect, ForwardIdempotency: true}, TimeoutMS: timeout}), 201))
	}
	wait := func(id string) tooling.Call {
		t.Helper()
		until := time.Now().Add(10 * time.Second)
		for time.Now().Before(until) {
			c := as[tooling.Call](t, request("GET", "/api/tool-calls/"+id, "", nil, 200))
			if c.State != "accepted" && c.State != "running" {
				return c
			}
			time.Sleep(50 * time.Millisecond)
		}
		t.Fatal("call wait exceeded")
		return tooling.Call{}
	}
	invoke := func(v tooling.ToolVersion, args string, mode, key string) tooling.Call {
		t.Helper()
		a := as[tooling.Accepted](t, request("POST", "/api/tool-calls", key, tooling.JSON(tooling.CallCreate{ToolVersionID: v.ToolVersionID, Arguments: []byte(args), Mode: mode}), 202))
		return wait(a.ToolCallID)
	}
	v := makeVersion("POST /items/{id}", "write", 30000)
	request("POST", "/api/tool-versions/"+v.ToolVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 409)
	args := `{"path":{"id":"가 나"},"query":{"id":["x y","x&y"]},"header":{"X-Label":"label"},"body":{"id":null}}`
	key := rpc.NewID()
	c := invoke(v, args, "test", key)
	if c.State != "succeeded" {
		t.Fatalf("HTTP failed: %+v", c)
	}
	result := as[map[string]json.RawMessage](t, c.Result)
	data := as[map[string]any](t, result["data"])
	if data["key"] != key || data["header"] != "label" || data["target"] != "/v1/items/%EA%B0%80%20%EB%82%98?id=x+y&id=x%26y" || data["body"].(map[string]any)["id"] != nil {
		t.Fatalf("serialization %s", c.Result)
	}
	request("POST", "/api/tool-versions/"+v.ToolVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 200)
	before := hits.Load()
	runKey := rpc.NewID()
	run := invoke(v, args, "run", runKey)
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	session, e := sdk.NewClient(&sdk.Implementation{Name: "a04-test", Version: "1"}, nil).Connect(ctx, &sdk.StreamableClientTransport{Endpoint: rest.URL + "/mcp", HTTPClient: &http.Client{Transport: authTransport{token}}}, nil)
	if e != nil {
		t.Fatal(e)
	}
	defer session.Close()
	mcpResult, e := session.CallTool(ctx, &sdk.CallToolParams{Name: "t_" + strings.ReplaceAll(v.ToolID, "-", "") + "_v1", Arguments: map[string]any{"invocation_key": runKey, "arguments": json.RawMessage(args)}})
	if e != nil || mcpResult.IsError {
		t.Fatalf("MCP %v %v", mcpResult, e)
	}
	replayed := as[tooling.Call](t, tooling.JSON(mcpResult.StructuredContent))
	if replayed.ToolCallID != run.ToolCallID || hits.Load() != before+1 {
		t.Fatal("REST/MCP duplicated HTTP effect")
	}
	for _, test := range []struct {
		operation, effect string
		timeout           int
		state, code       string
	}{
		{"POST /async", "write", 30000, "external_pending", "TOOL_ASYNC_UNSUPPORTED"}, {"POST /slow", "write", 1000, "unknown", "TOOL_EFFECT_UNKNOWN"}, {"POST /bad", "write", 30000, "unknown", "TOOL_EFFECT_UNKNOWN"}, {"GET /bad", "read", 30000, "failed", "TOOL_RESPONSE_INVALID"}, {"GET /redirect", "read", 30000, "failed", "TOOL_HTTP_ERROR"}, {"GET /empty", "read", 30000, "succeeded", ""}, {"GET /large", "read", 30000, "succeeded", ""},
	} {
		t.Run(test.operation+test.effect, func(t *testing.T) {
			version := makeVersion(test.operation, test.effect, test.timeout)
			key := rpc.NewID()
			call := invoke(version, `{}`, "test", key)
			if call.State != test.state || test.code != "" && (call.Error == nil || call.Error.Code != test.code) {
				t.Fatalf("unexpected %+v", call)
			}
			old := hits.Load()
			again := invoke(version, `{}`, "test", key)
			if again.ToolCallID != call.ToolCallID || hits.Load() != old {
				t.Fatal("terminal invocation replay executed again")
			}
		})
	}
	if script := os.Getenv("AGENT_TEST_OPENAPI_SMOKE_SCRIPT"); script != "" {
		cmd := exec.Command("python3", script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"))
		if out, e := cmd.CombinedOutput(); e != nil {
			t.Fatalf("OpenAPI smoke: %v %s", e, out)
		} else {
			t.Log(string(out))
		}
	}
	// Keep server/worker/fixture alive for an optional local browser acceptance.
	if script := os.Getenv("AGENT_TEST_BROWSER_SCRIPT"); script != "" {
		cmd := exec.Command("node", script)
		cmd.Env = append(os.Environ(), "AGENT_BROWSER_URL="+rest.URL, "AGENT_BROWSER_TOKEN="+token, "AGENT_FIXTURE_URL="+baseURL)
		out, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("browser: %v %s", e, out)
		}
		t.Log(string(out))
	}
	t.Logf("A04 real HTTP + Python + SQL + REST/MCP passed; persisted call %s; version %s", run.ToolCallID, run.ToolVersionID)
}
