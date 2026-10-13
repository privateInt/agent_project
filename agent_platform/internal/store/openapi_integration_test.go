package store_test

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/testdb"
	"agent-platform/internal/tooling"
	"bytes"
	"testing"
)

func TestOpenAPIManagementAndLeases(t *testing.T) {
	db := testdb.Open(t)
	ctx := testContext()
	source := []byte(`{"openapi":"3.0.3","info":{"title":"DB HTTP","version":"1"},"paths":{"/ping":{"post":{"responses":{"204":{"description":"done"}}}},"/later":{"post":{"responses":{"202":{"description":"accepted"}}}}}}`)
	out, e := db.ToolAPI(ctx, "CreateOpenAPIImport", &platformv1.ToolRequest{IdempotencyKey: rpc.NewID(), Json: source, DocumentFormat: "json"})
	if e != nil {
		t.Fatal(e)
	}
	imp := decode[store.OpenAPIImport](t, out.Json)
	out, e = db.ToolAPI(ctx, "CreateOpenAPIImport", &platformv1.ToolRequest{IdempotencyKey: rpc.NewID(), Json: source, DocumentFormat: "json"})
	if e != nil || decode[store.OpenAPIImport](t, out.Json).ImportID != imp.ImportID {
		t.Fatal("document dedupe", e)
	}
	var saved []byte
	if e = db.Pool.QueryRow(ctx, `SELECT source FROM agent.openapi_imports WHERE import_id=$1`, imp.ImportID).Scan(&saved); e != nil || !bytes.Equal(saved, source) {
		t.Fatal("raw document changed", e)
	}
	connection := decode[tooling.Connection](t, api(t, db, "CreateConnection", "", rpc.NewID(), tooling.ConnectionCreate{DisplayName: "pin", ConnectionSpec: tooling.ConnectionSpec{BaseURL: "https://api.example.test/v1", AuthType: "none"}}))
	tool := decode[tooling.Tool](t, api(t, db, "CreateTool", "", rpc.NewID(), tooling.ToolCreate{DisplayName: "HTTP"}))
	binding := tooling.Binding{ImportID: imp.ImportID, OperationKey: "POST /ping", ConnectionVersionID: connection.CurrentVersionID, AuthScheme: "none", Effect: "write"}
	body := tooling.VersionCreate{ExpectedRevision: 1, Kind: "openapi_http_v1", Binding: binding, TimeoutMS: 120000}
	body.Binding.OperationKey = "POST /later"
	_, e = db.ToolAPI(ctx, "CreateToolVersion", &platformv1.ToolRequest{ResourceId: tool.ToolID, IdempotencyKey: rpc.NewID(), Json: tooling.JSON(body)})
	if tooling.Code(e) != "OPENAPI_UNSUPPORTED" {
		t.Fatal("unsupported draft accepted", e)
	}
	body.Binding = binding
	version := decode[tooling.ToolVersion](t, api(t, db, "CreateToolVersion", tool.ToolID, rpc.NewID(), body))
	api(t, db, "CreateConnectionVersion", connection.ConnectionID, rpc.NewID(), tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "https://other.example.test/v2", AuthType: "none"}})
	key := rpc.NewID()
	accepted := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", key, tooling.CallCreate{ToolVersionID: version.ToolVersionID, Arguments: []byte(`{}`), Mode: "test"}))
	job, e := db.ClaimTool(ctx)
	if e != nil || job == nil {
		t.Fatal(e)
	}
	if job.Snapshot.Connection.BaseURL != "https://api.example.test/v1" || job.Snapshot.Connection.ConnectionVersionID != connection.CurrentVersionID {
		t.Fatal("connection snapshot drift")
	}
	if _, e = db.Pool.Exec(ctx, `UPDATE agent.tool_calls SET lease_until=clock_timestamp()-interval '1 second' WHERE tool_call_id=$1`, job.ID); e != nil {
		t.Fatal(e)
	}
	next, e := db.ClaimTool(ctx)
	if e != nil || next != nil {
		t.Fatal("expired HTTP job replayed", e)
	}
	call, e := db.ToolCall(ctx, accepted.ToolCallID)
	if e != nil || call.State != "unknown" || call.Error.Code != "TOOL_EFFECT_UNKNOWN" {
		t.Fatal(call, e)
	}
	if e = db.FinishTool(ctx, job, []byte(`{"data":null}`), ""); tooling.Code(e) != "TOOL_EXECUTION_FENCED" {
		t.Fatal("late report accepted", e)
	}
	repeated := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", key, tooling.CallCreate{ToolVersionID: version.ToolVersionID, Arguments: []byte(`{}`), Mode: "test"}))
	if repeated.ToolCallID != call.ToolCallID {
		t.Fatal("unknown call replay changed")
	}
}
