package app_test

import (
	gateway "agent-platform/gateway/app"
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/agenting"
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

func TestAgentEndToEnd(t *testing.T) {
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
	callbackAddress := os.Getenv("AGENT_TEST_SERVICE_ADDRESS")
	if callbackAddress == "" {
		callbackAddress = "127.0.0.1:0"
	}
	listener, e := net.Listen("tcp", callbackAddress)
	if e != nil {
		t.Fatal(e)
	}
	service := grpc.NewServer(grpc.UnaryInterceptor(rpc.Authenticate(token)), grpc.MaxRecvMsgSize(8<<20), grpc.MaxSendMsgSize(8<<20))
	platformv1.RegisterAgentServiceServer(service, &app.Agents{DB: db, Runtime: runtime})
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
	rest := httptest.NewServer((&gateway.Server{Client: platformv1.NewPlatformServiceClient(conn), Tools: tools, Agents: platformv1.NewAgentServiceClient(conn), Token: token, MCP: httputil.NewSingleHostReverseProxy(target)}).Handler())
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
	// Catalogs include retained test and operational records. Follow the public
	// cursor and inspect only records created by this invocation.
	catalogItems := func(path, idField string, ids ...string) []map[string]any {
		t.Helper()
		wanted := make(map[string]bool, len(ids))
		for _, id := range ids {
			wanted[id] = true
		}
		items := make([]map[string]any, 0, len(ids))
		cursor := ""
		seen := map[string]bool{}
		for len(wanted) > 0 {
			pagePath := path
			if cursor != "" {
				pagePath += "&cursor=" + url.QueryEscape(cursor)
			}
			page := as[tooling.Page](t, request("GET", pagePath, "", nil, 200))
			for _, raw := range page.Items {
				item := as[map[string]any](t, raw)
				id, _ := item[idField].(string)
				if wanted[id] {
					items = append(items, item)
					delete(wanted, id)
				}
			}
			if len(wanted) == 0 || page.NextCursor == "" {
				break
			}
			if seen[page.NextCursor] {
				t.Fatalf("%s repeated catalog cursor", path)
			}
			seen[page.NextCursor] = true
			cursor = page.NextCursor
		}
		if len(wanted) > 0 {
			t.Fatalf("%s missing %d records created by this test", path, len(wanted))
		}
		return items
	}
	workerCtx, stop := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); (&app.ToolWorker{DB: db, Runtime: runtime}).Run(workerCtx) }()
	agentDone := make(chan struct{})
	go func() { defer close(agentDone); (&app.AgentWorker{DB: db, Runtime: runtime}).Run(workerCtx) }()
	defer func() { stop(); <-done; <-agentDone }()
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
	chatDep, chatVersion, chatModel := rpc.NewID(), rpc.NewID(), rpc.NewID()
	var chatCalls atomic.Int64
	var emptySearch, badCitations, badModel, chatReject atomic.Bool
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
		case "GET /api/deployments":
			result = map[string]any{"items": []any{map[string]any{"deployment_id": chatDep, "serving": map[string]any{"engine": "vllm", "profile": ""}}}, "next_cursor": ""}
		case "GET /api/deployments/" + chatDep:
			mv := chatVersion
			if badModel.Load() {
				mv = rpc.NewID()
			}
			result = map[string]any{"deployment_id": chatDep, "model_version_id": mv, "status": "ready", "runtime_state": "ready", "desired_state": "running", "serving": map[string]any{"engine": "vllm", "profile": "", "max_model_len": 4096}, "served_model_name": "deployment-" + chatDep, "runtime_container_id": "a06-fixture", "runtime_image_id": "sha256:fixture"}
		case "GET /api/model-versions/" + chatVersion:
			result = map[string]any{"model_version_id": chatVersion, "model_id": chatModel, "version": "v1", "repo_id": "fixture/chat"}
		case "GET /api/models/" + chatModel:
			result = map[string]any{"model_id": chatModel, "model_type": "llm", "name": "A06 답변 모델"}
		case "POST /api/deployments/" + chatDep + "/chat":
			chatCalls.Add(1)
			if chatReject.Load() {
				w.WriteHeader(400)
				_ = json.NewEncoder(w).Encode(map[string]any{"code": "CHAT_INPUT_REJECTED"})
				return
			}
			var body struct {
				Messages []struct {
					Role    string `json:"role"`
					Content string `json:"content"`
				} `json:"messages"`
				MaxTokens   int     `json:"max_tokens"`
				Temperature float64 `json:"temperature"`
			}
			if json.NewDecoder(r.Body).Decode(&body) != nil || len(body.Messages) != 2 || body.Messages[0].Role != "system" || body.Messages[1].Role != "user" || body.MaxTokens != 512 {
				t.Error("chat request contract")
				w.WriteHeader(400)
				return
			}
			var data struct {
				Question string `json:"question"`
				Sources  []struct {
					ID   string `json:"id"`
					Text string `json:"text"`
				} `json:"sources"`
			}
			if json.Unmarshal([]byte(body.Messages[1].Content), &data) != nil || data.Question != "  자연어 질문  " || len(data.Sources) != 1 || data.Sources[0].ID != "S1" || data.Sources[0].Text != "본문과 출처 보존" {
				t.Error("question/source mapping")
				w.WriteHeader(400)
				return
			}
			answer := "자료의 본문과 출처를 확인했습니다. [S1]"
			if badCitations.Load() {
				answer = "없는 출처 [S9]"
			}
			result = map[string]any{"deployment_id": chatDep, "model": "deployment-" + chatDep, "content": answer, "finish_reason": "stop", "usage": map[string]any{"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40}}

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
			if emptySearch.Load() {
				obj := result.(map[string]any)
				obj["results"] = []any{}
				obj["returned_count"] = 0
				obj["candidate_count"] = 0
			}
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

	inspection := agenting.Inspection{Action: "deployments", ConnectionVersionID: model.CurrentVersionID}
	request("POST", "/api/agent-models/inspect", rpc.NewID(), tooling.JSON(inspection), 200)
	inspection.Action = "resolve"
	inspection.DeploymentID = chatDep
	resolvedModel := as[struct {
		Model agenting.Model `json:"model"`
	}](t, request("POST", "/api/agent-models/inspect", rpc.NewID(), tooling.JSON(inspection), 200))
	if chatCalls.Load() != 0 {
		t.Fatal("model discovery ran generation")
	}
	a := as[agenting.Agent](t, request("POST", "/api/agents", rpc.NewID(), tooling.JSON(tooling.ToolCreate{DisplayName: "A06 QA fixture"}), 201))
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		raw, e := db.ToolAPI(ctx, "GetAgent", &platformv1.ToolRequest{ResourceId: a.AgentID})
		if e != nil {
			t.Error(e)
			return
		}
		current := as[agenting.Agent](t, raw.Json)
		off := false
		_, e = db.ToolAPI(ctx, "UpdateAgent", &platformv1.ToolRequest{ResourceId: a.AgentID, Json: tooling.JSON(tooling.ToolPatch{ExpectedRevision: current.Revision, Enabled: &off})})
		if e != nil {
			t.Error(e)
		}
	})
	av := as[agenting.Version](t, request("POST", "/api/agents/"+a.AgentID+"/versions", rpc.NewID(), tooling.JSON(agenting.VersionCreate{ExpectedRevision: 1, Definition: agenting.Definition{ToolVersionID: v.ToolVersionID, Model: resolvedModel.Model, Temperature: 0.2}}), 201))
	request("POST", "/api/agent-versions/"+av.AgentVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 409)
	request("POST", "/api/workflow-links", rpc.NewID(), tooling.JSON(agenting.WorkflowLinkCreate{DisplayName: "draft", InstanceURL: "http://flows.example.test", WorkflowID: "draft", AgentVersionID: av.AgentVersionID}), 409)
	waitAgent := func(id string) agenting.Run {
		t.Helper()
		until := time.Now().Add(30 * time.Second)
		for time.Now().Before(until) {
			r := as[agenting.Run](t, request("GET", "/api/agent-runs/"+id, "", nil, 200))
			if r.State != "accepted" && r.State != "running" {
				return r
			}
			time.Sleep(100 * time.Millisecond)
		}
		t.Fatal("Agent wait expired")
		return agenting.Run{}
	}
	body := agenting.RunCreate{AgentVersionID: av.AgentVersionID, Input: agenting.Input{Question: "  자연어 질문  "}, Mode: "run"}
	request("POST", "/api/agent-runs", rpc.NewID(), tooling.JSON(body), 409)
	body.Mode = "test"
	key := rpc.NewID()
	accepted := as[agenting.Accepted](t, request("POST", "/api/agent-runs", key, tooling.JSON(body), 202))
	run := waitAgent(accepted.AgentRunID)
	if run.State != "succeeded" {
		t.Fatalf("Agent test failed: %s", tooling.JSON(run))
	}
	if run.ToolCallID == nil || !bytes.Contains(run.Result, []byte("[S1]")) || !bytes.Contains(run.Result, []byte("future_field")) {
		t.Fatal("Agent result lost source or Tool linkage")
	}
	child := as[tooling.Call](t, request("GET", "/api/tool-calls/"+*run.ToolCallID, "", nil, 200))
	if child.ParentAgentRunID == nil || *child.ParentAgentRunID != run.AgentRunID || child.RequestID != run.RequestID {
		t.Fatal("child correlation lost")
	}
	assertAgentTrace(t, run)
	steps := as[struct {
		Items []agenting.Step `json:"items"`
	}](t, request("GET", "/api/agent-runs/"+run.AgentRunID+"/steps", "", nil, 200))
	if len(steps.Items) != 5 {
		t.Fatal("missing steps")
	}
	for _, step := range steps.Items {
		if step.State != "succeeded" {
			t.Fatalf("step incomplete %+v", step)
		}
	}
	request("POST", "/api/agent-versions/"+av.AgentVersionID+"/publish", rpc.NewID(), tooling.JSON(tooling.Revision{ExpectedRevision: 1}), 200)
	// A07 references are Agent-owned metadata; no n8n URL is contacted.
	linkBody := agenting.WorkflowLinkCreate{DisplayName: "A07 fixture Flow", InstanceURL: "http://flows.example.test:16200/", WorkflowID: "flow-" + rpc.NewID(), AgentVersionID: av.AgentVersionID}
	linkKey := rpc.NewID()
	link := as[agenting.WorkflowLink](t, request("POST", "/api/workflow-links", linkKey, tooling.JSON(linkBody), 201))
	replayLink := as[agenting.WorkflowLink](t, request("POST", "/api/workflow-links", linkKey, tooling.JSON(linkBody), 201))
	if link.WorkflowLinkID != replayLink.WorkflowLinkID || link.EditURL != "http://flows.example.test:16200/workflow/"+link.WorkflowID {
		t.Fatal("workflow link replay/edit URL")
	}
	request("POST", "/api/workflow-links", rpc.NewID(), tooling.JSON(linkBody), 409)
	linkBody.DisplayName = "conflict"
	request("POST", "/api/workflow-links", linkKey, tooling.JSON(linkBody), 409)
	request("GET", "/api/workflow-links/"+link.WorkflowLinkID, "", nil, 200)
	linkSummary := catalogItems("/api/workflow-links?limit=100", "workflow_link_id", link.WorkflowLinkID)[0]
	if linkSummary["agent_id"] != a.AgentID || linkSummary["agent_display_name"] != a.DisplayName || linkSummary["agent_version_state"] != "published" || linkSummary["agent_enabled"] != true {
		t.Fatal("workflow catalog did not resolve pinned Agent metadata")
	}
	request("GET", "/api/workflow-links?cursor=bad", "", nil, 400)
	request("GET", "/api/workflow-links/"+rpc.NewID(), "", nil, 404)
	request("PATCH", "/api/workflow-links/"+link.WorkflowLinkID, "", tooling.JSON(map[string]any{"expected_revision": 1, "enabled": false}), 200)
	request("PATCH", "/api/workflow-links/"+link.WorkflowLinkID, "", tooling.JSON(map[string]any{"expected_revision": 1, "enabled": true}), 409)
	linkBody.InstanceURL = "javascript:alert(1)"
	linkBody.WorkflowID = rpc.NewID()
	request("POST", "/api/workflow-links", rpc.NewID(), tooling.JSON(linkBody), 400)

	replay := as[agenting.Accepted](t, request("POST", "/api/agent-runs", key, tooling.JSON(body), 202))
	if replay.AgentRunID != run.AgentRunID || chatCalls.Load() != 1 {
		t.Fatal("replayed Agent executed again")
	}
	assertAgentTrace(t, run)
	body.Input.Question = "different"
	request("POST", "/api/agent-runs", key, tooling.JSON(body), 409)
	body.Input.Question = "  자연어 질문  "
	body.Mode = "run"
	invokeAgent := func() agenting.Run {
		t.Helper()
		accepted := as[agenting.Accepted](t, request("POST", "/api/agent-runs", rpc.NewID(), tooling.JSON(body), 202))
		return waitAgent(accepted.AgentRunID)
	}
	// Moving connection pointers preserves both search and generation snapshots.
	for _, c := range []tooling.Connection{knowledge, model} {
		request("POST", "/api/connections/"+c.ConnectionID+"/versions", rpc.NewID(), tooling.JSON(tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "https://changed.example.test", AuthType: "none"}}), 201)
	}
	ok := invokeAgent()
	if ok.State != "succeeded" || chatCalls.Load() != 2 {
		t.Fatalf("published run failed %s", tooling.JSON(ok))
	}
	emptySearch.Store(true)
	empty := invokeAgent()
	emptySearch.Store(false)
	if empty.State != "succeeded" || !bytes.Contains(empty.Result, []byte("no_evidence")) || chatCalls.Load() != 2 {
		t.Fatalf("empty branch generated: %s", tooling.JSON(empty))
	}
	badCitations.Store(true)
	body.CallerContext = tooling.CallerContext{System: "n8n", WorkflowID: link.WorkflowID, ExecutionID: "fixture-execution", NodeID: "agent-call"}
	invalid := invokeAgent()
	body.CallerContext = tooling.CallerContext{}
	badCitations.Store(false)
	if invalid.State != "failed" || invalid.Error == nil || invalid.Error.Code != "ANSWER_REFERENCE_INVALID" || chatCalls.Load() != 3 {
		t.Fatalf("citation check failed %s", tooling.JSON(invalid))
	}
	badModel.Store(true)
	mismatch := invokeAgent()
	badModel.Store(false)
	if mismatch.State != "failed" || mismatch.Error.Code != "MODEL_IDENTITY_MISMATCH" || chatCalls.Load() != 3 {
		t.Fatalf("model pin failed %s", tooling.JSON(mismatch))
	}
	wrongSpace.Store(true)
	searchFailure := invokeAgent()
	wrongSpace.Store(false)
	if searchFailure.State != "failed" || searchFailure.Error.Code != "AGENT_SEARCH_FAILED" || searchFailure.ToolCallID == nil || chatCalls.Load() != 3 {
		t.Fatalf("search failure generated %s", tooling.JSON(searchFailure))
	}
	chatReject.Store(true)
	rejected := invokeAgent()
	chatReject.Store(false)
	if rejected.State != "failed" || rejected.Error.Code != "MODEL_INPUT_REJECTED" || chatCalls.Load() != 4 {
		t.Fatalf("chat rejection lost %s", tooling.JSON(rejected))
	}
	runSummaries := catalogItems("/api/agent-runs?limit=100", "agent_run_id",
		run.AgentRunID, ok.AgentRunID, empty.AgentRunID, invalid.AgentRunID,
		mismatch.AgentRunID, searchFailure.AgentRunID, rejected.AgentRunID)
	foundInvalid, foundSuccess := false, false
	for _, summary := range runSummaries {
		if summary["agent_id"] != a.AgentID || summary["agent_version_id"] != av.AgentVersionID || summary["agent_version"] != float64(1) {
			t.Fatal("run summary lost pinned Agent identity")
		}
		if _, leaked := summary["snapshot"]; leaked {
			t.Fatal("run summary exposes execution snapshot")
		}
		if summary["agent_run_id"] == invalid.AgentRunID {
			foundInvalid = true
			caller := summary["caller_context"].(map[string]any)
			err := summary["error"].(map[string]any)
			if caller["system"] != "n8n" || caller["workflow_id"] != link.WorkflowID || err["code"] != "ANSWER_REFERENCE_INVALID" || err["stage"] != "finalize" {
				t.Fatal("run summary lost original caller or finalization failure")
			}
		}
		if summary["agent_run_id"] == ok.AgentRunID {
			foundSuccess = true
			if summary["error"] != nil {
				t.Fatal("successful summary contains an error")
			}
		}
	}
	if !foundInvalid || !foundSuccess {
		t.Fatal("missing failed or successful run summaries")
	}

	if script := os.Getenv("AGENT_TEST_AGENT_SMOKE_SCRIPT"); script != "" {
		before := chatCalls.Load()
		stateFile := t.TempDir() + "/agent-state.json"
		for range 2 {
			cmd := exec.Command("python3", script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"), "--agent-name", a.DisplayName, "--question", "  자연어 질문  ", "--state-file", stateFile)
			if output, e := cmd.CombinedOutput(); e != nil {
				t.Fatalf("Agent smoke: %v %s", e, output)
			}
		}
		if chatCalls.Load() != before+1 {
			t.Fatal("Agent smoke replay repeated generation")
		}
	}
	if script := os.Getenv("AGENT_TEST_A09_SCRIPT"); script != "" {
		beforeChat, beforeEmbedding, beforeSearch := chatCalls.Load(), embeddingCalls.Load(), searchCalls.Load()
		folder := t.TempDir()
		for _, action := range []string{"capture", "compare"} {
			args := []string{script, action, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"),
				"--session-dir", folder + "/absent", "--baseline-file", folder + "/a09.json"}
			if action == "capture" {
				args = append(args, "--run-id", ok.AgentRunID, "--run-id", invalid.AgentRunID)
			}
			output, e := exec.Command("python3", args...).CombinedOutput()
			if e != nil {
				t.Fatalf("A09 Agent record checks: %v %s", e, output)
			}
			t.Log(string(output))
		}
		if chatCalls.Load() != beforeChat || embeddingCalls.Load() != beforeEmbedding || searchCalls.Load() != beforeSearch {
			t.Fatal("A09 read-only inspection executed inference")
		}
	}

	if script := os.Getenv("AGENT_TEST_N8N_SCRIPT"); script != "" {
		before := chatCalls.Load()
		cmd := exec.Command("node", script)
		cmd.Env = append(os.Environ(), "A07_AGENT_URL="+rest.URL, "A07_AGENT_VERSION="+av.AgentVersionID, "A07_AGENT_TOKEN_FILE="+os.Getenv("AGENT_INTERNAL_TOKEN_FILE"))
		output, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("n8n wire: %v %s", e, output)
		}
		t.Log(string(output))
		if chatCalls.Load() != before+1 {
			t.Fatal("n8n workflow replay repeated generation")
		}
	}
	if script := os.Getenv("AGENT_TEST_N8N_OPERATIONAL_SCRIPT"); script != "" {
		beforeChat, beforeEmbedding, beforeSearch := chatCalls.Load(), embeddingCalls.Load(), searchCalls.Load()
		cmd := exec.Command("python3", script)
		cmd.Env = append(os.Environ(), "A07_AGENT_URL="+rest.URL, "A07_AGENT_VERSION="+av.AgentVersionID, "A07_AGENT_TOKEN_FILE="+os.Getenv("AGENT_INTERNAL_TOKEN_FILE"))
		output, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("operational n8n: %v %s", e, output)
		}
		t.Log(string(output))
		if chatCalls.Load() != beforeChat+1 || embeddingCalls.Load() != beforeEmbedding+1 || searchCalls.Load() != beforeSearch+1 {
			t.Fatal("operational workflow replay/lookup repeated external effects")
		}
	}
	if script := os.Getenv("AGENT_TEST_N8N_LIVE_SCRIPT"); script != "" {
		beforeChat, beforeEmbedding, beforeSearch := chatCalls.Load(), embeddingCalls.Load(), searchCalls.Load()
		// Lose all three initial POST responses after Agent commits. The next
		// process must reuse its on-disk request, including original caller.
		target, _ := url.Parse(rest.URL)
		proxy := httputil.NewSingleHostReverseProxy(target)
		var lost atomic.Int32
		proxy.ModifyResponse = func(response *http.Response) error {
			if response.Request.Method == "POST" && response.Request.URL.Path == "/api/agent-runs" && lost.Add(1) <= 3 {
				response.Body.Close()
				return fmt.Errorf("intentional lost acceptance response")
			}
			return nil
		}
		proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) { w.WriteHeader(http.StatusBadGateway) }
		wire := httptest.NewServer(proxy)
		defer wire.Close()
		session := t.TempDir()
		if err := os.Chmod(session, 0700); err != nil {
			t.Fatal(err)
		}
		prepare := exec.Command("python3", os.Getenv("AGENT_TEST_N8N_LIVE_PREPARE"), "prepare", "--non-interactive", "--session-dir", session, "--agent-url", wire.URL, "--container-agent-url", wire.URL, "--new-agent", "--tool-version-id", v.ToolVersionID, "--model-connection-version-id", model.CurrentVersionID, "--deployment-id", chatDep, "--question", "  자연어 질문  ", "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE"))
		if output, err := prepare.CombinedOutput(); err != nil {
			t.Fatalf("live preparation: %v %s", err, output)
		}
		for index, action := range []string{"run", "run", "run", "lookup"} {
			cmd := exec.Command("node", script)
			cmd.Env = append(os.Environ(), "AGENT_LIVE_SESSION="+session)
			cmd.Stdin = bytes.NewReader(tooling.JSON(map[string]string{"token": token, "action": action}))
			output, err := cmd.CombinedOutput()
			if index == 0 {
				if err == nil || lost.Load() != 3 {
					t.Fatalf("expected lost responses: %v %s", err, output)
				}
				if _, e := os.Stat(session + "/request.json"); e != nil {
					t.Fatal("request was not durable before POST", e)
				}
				if _, e := os.Stat(session + "/accepted.json"); !os.IsNotExist(e) {
					t.Fatal("lost response incorrectly marked accepted")
				}
				t.Log("all three acceptance responses lost; durable request retained")
				continue
			}
			if err != nil {
				log, _ := os.ReadFile(session + "/last-cli-log.json")
				t.Fatalf("live n8n %s: %v %s; private fixture log %s", action, err, output, log)
			}
			t.Log(string(output))
		}
		t.Log("resume/replay/lookup inference deltas: embedding=1 search=1 chat=1")
		if chatCalls.Load() != beforeChat+1 || embeddingCalls.Load() != beforeEmbedding+1 || searchCalls.Load() != beforeSearch+1 {
			t.Fatal("live resume/replay/lookup duplicated model or search calls")
		}
	}
	if script := os.Getenv("AGENT_TEST_A08_SMOKE_SCRIPT"); script != "" {
		beforeChat, beforeSearch, beforeEmbedding := chatCalls.Load(), searchCalls.Load(), embeddingCalls.Load()
		for _, extra := range [][]string{{"--before-update"}, {"--run-id", invalid.AgentRunID, "--workflow-link-id", link.WorkflowLinkID, "--expect-error", "ANSWER_REFERENCE_INVALID"}} {
			args := append([]string{script, "--url", rest.URL, "--token-file", os.Getenv("AGENT_INTERNAL_TOKEN_FILE")}, extra...)
			output, e := exec.Command("python3", args...).CombinedOutput()
			if e != nil {
				t.Fatalf("A08 read-only smoke: %v %s", e, output)
			}
			t.Log(string(output))
		}
		if chatCalls.Load() != beforeChat || searchCalls.Load() != beforeSearch || embeddingCalls.Load() != beforeEmbedding {
			t.Fatal("read-only console smoke executed inference")
		}
	}
	if script := os.Getenv("AGENT_TEST_AGENT_BROWSER_SCRIPT"); script != "" {
		before := chatCalls.Load()
		cmd := exec.Command("node", script)
		cmd.Env = append(os.Environ(), "AGENT_BROWSER_URL="+rest.URL, "AGENT_BROWSER_TOKEN="+token)
		output, e := cmd.CombinedOutput()
		if e != nil {
			t.Fatalf("browser: %v %s", e, output)
		}
		t.Log(string(output))
		if chatCalls.Load() != before+3 {
			t.Fatal("browser recovery duplicated generation")
		}
	}
	// After stopping workers, exercise accepted recovery, lease loss and late reports.
	stop()
	<-done
	<-agentDone
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	queuedKey := rpc.NewID()
	queued := as[agenting.Accepted](t, request("POST", "/api/agent-runs", queuedKey, tooling.JSON(body), 202))
	job, e := db.ClaimAgent(ctx)
	if e != nil || job == nil || job.ID != queued.AgentRunID {
		t.Fatal("durable accepted job missing", e)
	}
	other, e := db.ClaimAgent(ctx)
	if e != nil || other != nil {
		t.Fatal("double claim", e)
	}
	_, e = db.AgentCommand(ctx, &platformv1.AgentCommand{AgentRunId: job.ID, LeaseToken: job.LeaseToken, Operation: "start", StepId: "generate", Json: []byte(`{}`)})
	if tooling.Code(e) != "AGENT_EXECUTION_FENCED" {
		t.Fatal("out-of-order model step accepted", e)
	}
	if e = db.HeartbeatAgent(ctx, job); e != nil {
		t.Fatal(e)
	}
	_, e = db.Pool.Exec(ctx, `UPDATE agent.agent_runs SET lease_until=clock_timestamp()-interval '1 second' WHERE agent_run_id=$1`, job.ID)
	if e != nil {
		t.Fatal(e)
	}
	if recovered, e := db.ClaimAgent(ctx); e != nil || recovered != nil {
		t.Fatal("running job replayed", e)
	}
	interrupted, e := db.AgentRun(ctx, job.ID)
	if e != nil || interrupted.State != "interrupted" {
		t.Fatal("lease expiry not persisted", e)
	}
	if e = db.FinishAgent(ctx, job, nil, "AGENT_TIMEOUT", "execute"); tooling.Code(e) != "AGENT_EXECUTION_FENCED" {
		t.Fatal("late report accepted", e)
	}
	expired := as[agenting.Accepted](t, request("POST", "/api/agent-runs", rpc.NewID(), tooling.JSON(body), 202))
	_, e = db.Pool.Exec(ctx, `UPDATE agent.agent_runs SET deadline_at=clock_timestamp()-interval '1 second' WHERE agent_run_id=$1`, expired.AgentRunID)
	if e != nil {
		t.Fatal(e)
	}
	if next, e := db.ClaimAgent(ctx); e != nil || next != nil {
		t.Fatal("expired queued job executed", e)
	}
	failed, e := db.AgentRun(ctx, expired.AgentRunID)
	if e != nil || failed.State != "failed" || failed.Error.Code != "AGENT_TIMEOUT" {
		t.Fatal("queue timeout missing", e)
	}
	// Lost finish reports can be repeated without invoking the runtime again.
	report := as[agenting.Accepted](t, request("POST", "/api/agent-runs", rpc.NewID(), tooling.JSON(body), 202))
	reporting, e := db.ClaimAgent(ctx)
	if e != nil || reporting == nil || reporting.ID != report.AgentRunID {
		t.Fatal(e)
	}
	for range 2 {
		if e = db.FinishAgent(ctx, reporting, nil, "AGENT_SEARCH_FAILED", "search"); e != nil {
			t.Fatal("report replay rejected", e)
		}
	}
	last := as[agenting.Agent](t, request("GET", "/api/agents/"+a.AgentID, "", nil, 200))
	off := false
	request("PATCH", "/api/agents/"+a.AgentID, "", tooling.JSON(tooling.ToolPatch{ExpectedRevision: last.Revision, Enabled: &off}), 200)
	replayAfterDisable := as[agenting.Accepted](t, request("POST", "/api/agent-runs", queuedKey, tooling.JSON(body), 202))
	if replayAfterDisable.AgentRunID != queued.AgentRunID {
		t.Fatal("disable lost old invocation")
	}
	request("POST", "/api/agent-runs", rpc.NewID(), tooling.JSON(body), 409)
	t.Logf("A06 real Go/Python/SQL integration passed; chat requests=%d", chatCalls.Load())
}
