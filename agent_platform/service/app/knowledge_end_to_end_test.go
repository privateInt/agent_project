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

func TestKnowledgeEndToEnd(t *testing.T) {
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
	platformv1.RegisterToolServiceServer(service, &app.Tools{DB: db, Runtime: runtime})
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
	indexID, versionID, deploymentID, modelVersionID := rpc.NewID(), rpc.NewID(), rpc.NewID(), rpc.NewID()
	sourceID, canonicalID := rpc.NewID(), rpc.NewID()
	space := "es_" + strings.Repeat("a", 64)
	var embeddingCalls, searchCalls atomic.Int64
	var wrongSpace, changedActive atomic.Bool
	profile := func() map[string]any {
		actual := space
		if wrongSpace.Load() {
			actual = "es_" + strings.Repeat("c", 64)
		}
		return map[string]any{"deployment_id": deploymentID, "model_version_id": modelVersionID, "embedding_space_id": actual, "dimension": 1024, "normalization": "l2", "capability": "embedding", "status": "ready", "truncation": "reject", "max_input_tokens": 8192, "max_input_bytes": 131072}
	}
	fixture := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		var result any
		current := versionID
		if changedActive.Load() {
			current = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
		}
		index := map[string]any{"index_id": indexID, "name": "A05 knowledge", "active_version_id": current, "state": "active"}
		switch r.Method + " " + r.URL.Path {
		case "GET /api/indexes":
			result = map[string]any{"items": []any{index}}
		case "GET /api/indexes/" + indexID:
			result = index
		case "GET /api/index-versions/" + versionID:
			p := profile()
			p["embedding_space_id"] = space
			result = map[string]any{"index_id": indexID, "index_version_id": versionID, "embedding_profile": p, "state": "ready", "reference_state": "held", "chunk_count": 1, "indexed_count": 1}
		case "GET /api/deployments/" + deploymentID + "/embedding-profile":
			result = profile()
		case "POST /api/deployments/" + deploymentID + "/embeddings":
			embeddingCalls.Add(1)
			var body struct {
				Space  string `json:"expected_embedding_space_id"`
				Role   string `json:"role"`
				Inputs []struct {
					ID   string `json:"id"`
					Text string `json:"text"`
				} `json:"inputs"`
			}
			if json.NewDecoder(r.Body).Decode(&body) != nil || body.Space != space || body.Role != "query" || len(body.Inputs) != 1 || body.Inputs[0].Text != "  자연어 질문  " {
				t.Error("embedding request contract")
				http.Error(w, "invalid", 400)
				return
			}
			vector := make([]float64, 1024)
			vector[0] = 1
			result = map[string]any{"profile": profile(), "role": "query", "data": []any{map[string]any{"id": body.Inputs[0].ID, "index": 0, "embedding": vector, "input_tokens": 5}}}
		case "POST /api/indexes/" + indexID + "/search":
			searchCalls.Add(1)
			var body map[string]any
			if json.NewDecoder(r.Body).Decode(&body) != nil || body["index_version_id"] != versionID || body["embedding_space_id"] != space || body["query_text"] != nil || body["rerank"] != nil {
				t.Error("search request contract")
				http.Error(w, "invalid", 400)
				return
			}
			vector, ok := body["query_embedding"].([]any)
			if !ok || len(vector) != 1024 || vector[0] != float64(1) {
				t.Error("vector mapping")
				http.Error(w, "invalid", 400)
				return
			}
			result = map[string]any{"index_id": indexID, "index_version_id": versionID, "embedding_space_id": space, "top_k": 5, "candidate_k": 5, "candidate_count": 1, "returned_count": 1, "filters": map[string]any{}, "rerank": map[string]any{"requested": false, "applied": false, "status": "disabled"}, "results": []any{map[string]any{"chunk_id": strings.Repeat("b", 64), "source_document_id": sourceID, "canonical_id": canonicalID, "kind": "text", "text": "본문과 출처 보존", "rank": 1, "retrieval_rank": 1, "retrieval_score": 0.9, "source_spans": []any{map[string]any{"block_id": "p1", "metadata": map[string]any{"location": map[string]any{"page": 3}}}}, "quality": map[string]any{"warnings": []any{}}, "future_field": "preserved"}}}
		default:
			t.Errorf("unexpected external request %s %s", r.Method, r.URL.Path)
			http.Error(w, "invalid", 404)
			return
		}
		_ = json.NewEncoder(w).Encode(result)
	}))
	l, e := net.Listen("tcp", "0.0.0.0:0")
	if e != nil {
		t.Fatal(e)
	}
	fixture.Listener = l
	fixture.Start()
	defer fixture.Close()
	base := fmt.Sprintf("http://%s:%d", host, l.Addr().(*net.TCPAddr).Port)
	makeConn := func(name string) tooling.Connection {
		return as[tooling.Connection](t, request("POST", "/api/connections", rpc.NewID(), tooling.JSON(tooling.ConnectionCreate{DisplayName: name, ConnectionSpec: tooling.ConnectionSpec{BaseURL: base, AuthType: "none"}}), 201))
	}
	knowledge, model := makeConn("A05 Knowledge fixture"), makeConn("A05 Model fixture")
	inspect := tooling.KnowledgeInspect{Action: "indexes", ConnectionVersionID: knowledge.CurrentVersionID, ModelConnectionVersionID: model.CurrentVersionID}
	request("POST", "/api/knowledge/inspect", rpc.NewID(), tooling.JSON(inspect), 200)
	inspect.Action = "resolve"
	inspect.IndexID = indexID
	resolved := as[struct {
		Binding tooling.Binding `json:"binding"`
	}](t, request("POST", "/api/knowledge/inspect", rpc.NewID(), tooling.JSON(inspect), 200))
	if embeddingCalls.Load() != 0 || searchCalls.Load() != 0 {
		t.Fatal("discovery ran inference")
	}
	tool := as[tooling.Tool](t, request("POST", "/api/tools", rpc.NewID(), tooling.JSON(tooling.ToolCreate{DisplayName: "A05 natural search"}), 201))
	testdb.DisableOnCleanup(t, db, tool.ToolID)
	create := tooling.VersionCreate{ExpectedRevision: 1, Kind: "knowledge_search_v1", Binding: resolved.Binding, TimeoutMS: 180000}
	v := as[tooling.ToolVersion](t, request("POST", "/api/tools/"+tool.ToolID+"/versions", rpc.NewID(), tooling.JSON(create), 201))
	request("POST", "/api/tool-versions/"+v.ToolVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 409)
	args := json.RawMessage(`{"query":"  자연어 질문  "}`)
	wait := func(id string) tooling.Call {
		t.Helper()
		until := time.Now().Add(15 * time.Second)
		for time.Now().Before(until) {
			c := as[tooling.Call](t, request("GET", "/api/tool-calls/"+id, "", nil, 200))
			if c.State != "accepted" && c.State != "running" {
				return c
			}
			time.Sleep(50 * time.Millisecond)
		}
		t.Fatal("call wait expired")
		return tooling.Call{}
	}
	invoke := func(version, mode, key string) tooling.Call {
		t.Helper()
		a := as[tooling.Accepted](t, request("POST", "/api/tool-calls", key, tooling.JSON(tooling.CallCreate{ToolVersionID: version, Mode: mode, Arguments: args}), 202))
		return wait(a.ToolCallID)
	}
	request("POST", "/api/tool-calls", rpc.NewID(), tooling.JSON(tooling.CallCreate{ToolVersionID: v.ToolVersionID, Mode: "test", Arguments: []byte(`{"query":"x","top_k":0}`)}), 400)
	test := invoke(v.ToolVersionID, "test", rpc.NewID())
	if test.State != "succeeded" {
		t.Fatalf("test failed %+v", test)
	}
	if bytes.Contains(test.Result, []byte("query_embedding")) || !bytes.Contains(test.Result, []byte("future_field")) {
		t.Fatal("output lost data or leaked vector")
	}
	request("POST", "/api/tool-versions/"+v.ToolVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 200)
	// Moving active/current pointers cannot rewrite the published binding.
	changedActive.Store(true)
	for _, c := range []tooling.Connection{knowledge, model} {
		request("POST", "/api/connections/"+c.ConnectionID+"/versions", rpc.NewID(), tooling.JSON(tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "https://changed.example.test", AuthType: "none"}}), 201)
	}
	runKey := rpc.NewID()
	run := invoke(v.ToolVersionID, "run", runKey)
	if run.State != "succeeded" {
		t.Fatalf("pinned run failed %+v", run)
	}
	replay := invoke(v.ToolVersionID, "run", runKey)
	if replay.ToolCallID != run.ToolCallID {
		t.Fatal("REST replay duplicated")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	session, e := sdk.NewClient(&sdk.Implementation{Name: "a05-test", Version: "1"}, nil).Connect(ctx, &sdk.StreamableClientTransport{Endpoint: rest.URL + "/mcp", HTTPClient: &http.Client{Transport: authTransport{token}}}, nil)
	if e != nil {
		t.Fatal(e)
	}
	defer session.Close()
	name := "t_" + strings.ReplaceAll(tool.ToolID, "-", "") + "_v1"
	got, e := session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": runKey, "arguments": args}})
	if e != nil || got.IsError {
		t.Fatalf("MCP replay %v %v", got, e)
	}
	same := as[tooling.Call](t, tooling.JSON(got.StructuredContent))
	if same.ToolCallID != run.ToolCallID || !bytes.Equal(tooling.JSON(as[any](t, same.Result)), tooling.JSON(as[any](t, run.Result))) || embeddingCalls.Load() != 2 || searchCalls.Load() != 2 {
		t.Fatal("REST/MCP result/replay mismatch")
	}
	fresh, e := session.CallTool(ctx, &sdk.CallToolParams{Name: name, Arguments: map[string]any{"invocation_key": rpc.NewID(), "arguments": args}})
	if e != nil || fresh.IsError || embeddingCalls.Load() != 3 || searchCalls.Load() != 3 {
		t.Fatal("fresh MCP call", e)
	}
	wrongSpace.Store(true)
	failed := invoke(v.ToolVersionID, "run", rpc.NewID())
	if failed.State != "failed" || failed.Error == nil || failed.Error.Code != "KNOWLEDGE_PROFILE_MISMATCH" || failed.Error.Stage != "embedding_profile" || embeddingCalls.Load() != 3 || searchCalls.Load() != 3 {
		t.Fatalf("profile mismatch did not stop inference %+v", failed)
	}
	if script := os.Getenv("AGENT_TEST_KNOWLEDGE_SMOKE_SCRIPT"); script != "" {
		wrongSpace.Store(false)
		before := embeddingCalls.Load()
		statePath := t.TempDir() + "/smoke.json"
		for range 2 {
			cmd := exec.Command("python3", script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"), "--tool-name", tool.DisplayName, "--question", "  자연어 질문  ", "--state-file", statePath)
			if output, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("smoke: %v %s", err, output)
			}
		}
		if embeddingCalls.Load() != before+1 {
			t.Fatal("smoke resume duplicated inference")
		}
	}
	if script := os.Getenv("AGENT_TEST_KNOWLEDGE_BROWSER_SCRIPT"); script != "" {
		wrongSpace.Store(false)
		changedActive.Store(false)
		cmd := exec.Command("node", script)
		cmd.Env = append(os.Environ(), "AGENT_BROWSER_URL="+rest.URL, "AGENT_BROWSER_TOKEN="+token, "AGENT_FIXTURE_URL="+base)
		if output, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("browser: %v %s", err, output)
		} else {
			t.Log(string(output))
		}
	}
	t.Logf("A05 HTTP fixtures + Python + SQL + REST/MCP passed; call=%s request=%s", run.ToolCallID, run.RequestID)
}
