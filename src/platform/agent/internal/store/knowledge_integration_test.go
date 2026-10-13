package store_test

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/testdb"
	"agent-platform/internal/tooling"
	"strings"
	"testing"
)

func TestKnowledgeDatabase(t *testing.T) {
	db := testdb.Open(t)
	ctx := testContext()
	makeConn := func(name string) tooling.Connection {
		return decode[tooling.Connection](t, api(t, db, "CreateConnection", "", rpc.NewID(), tooling.ConnectionCreate{DisplayName: name, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "http://" + name + ":8000", AuthType: "none"}}))
	}
	knowledge, model := makeConn("knowledge-gateway"), makeConn("model-gateway")
	tool := decode[tooling.Tool](t, api(t, db, "CreateTool", "", rpc.NewID(), tooling.ToolCreate{DisplayName: "A05 pin test"}))
	b := tooling.Binding{ConnectionVersionID: knowledge.CurrentVersionID, Knowledge: &tooling.KnowledgeBinding{ModelConnectionVersionID: model.CurrentVersionID, IndexID: rpc.NewID(), IndexVersionID: rpc.NewID(), EmbeddingDeploymentID: rpc.NewID(), EmbeddingSpaceID: "es_" + strings.Repeat("a", 64), TopK: 5, CandidateK: 5}}
	version := decode[tooling.ToolVersion](t, api(t, db, "CreateToolVersion", tool.ToolID, rpc.NewID(), tooling.VersionCreate{ExpectedRevision: 1, Kind: "knowledge_search_v1", Binding: b}))
	if version.TimeoutMS != 180000 {
		t.Fatal("wrong default timeout")
	}
	_, err := db.ToolAPI(ctx, "PublishToolVersion", &platformv1.ToolRequest{ResourceId: version.ToolVersionID, IdempotencyKey: rpc.NewID(), Json: tooling.JSON(tooling.Revision{ExpectedRevision: 1})})
	if tooling.Code(err) != "TOOL_TEST_REQUIRED" {
		t.Fatal("untested knowledge version published", err)
	}
	for _, c := range []tooling.Connection{knowledge, model} {
		api(t, db, "CreateConnectionVersion", c.ConnectionID, rpc.NewID(), tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "https://changed.example.test", AuthType: "none"}})
	}
	body := tooling.CallCreate{ToolVersionID: version.ToolVersionID, Mode: "test", Arguments: []byte(`{"query":"질문"}`)}
	key := rpc.NewID()
	accepted := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", key, body))
	job, err := db.ClaimTool(ctx)
	if err != nil || job == nil {
		t.Fatal(err)
	}
	if job.Snapshot.Connection.ConnectionVersionID != knowledge.CurrentVersionID || job.Snapshot.ModelConnection.ConnectionVersionID != model.CurrentVersionID || job.Snapshot.ModelConnection.BaseURL != "http://model-gateway:8000" {
		t.Fatal("connection snapshots drifted")
	}
	if err = db.FinishTool(ctx, job, []byte(`{"data":null,"completion":"failed","stage":"embedding","timings_ms":{"embedding":2}}`), "TOOL_RESPONSE_INVALID"); err != nil {
		t.Fatal(err)
	}
	call, err := db.ToolCall(ctx, accepted.ToolCallID)
	if err != nil || call.Error == nil || call.Error.Stage != "embedding" || len(call.Result) == 0 {
		t.Fatal("failure stage lost", call, err)
	}
	replay := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", key, body))
	if replay.ToolCallID != accepted.ToolCallID {
		t.Fatal("failed call replay changed")
	}
	if next, e := db.ClaimTool(ctx); e != nil || next != nil {
		t.Fatal("failed execution retried", e)
	}
}
