package store_test

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/testdb"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"google.golang.org/grpc/metadata"
	"sync"
	"testing"
)

func testContext() context.Context {
	return metadata.NewIncomingContext(context.Background(), metadata.Pairs("x-request-id", rpc.NewID()))
}
func api(t *testing.T, db *store.Store, op, id, key string, body any) []byte {
	t.Helper()
	out, err := db.ToolAPI(testContext(), op, &platformv1.ToolRequest{ResourceId: id, IdempotencyKey: key, Json: tooling.JSON(body)})
	if err != nil {
		t.Fatalf("%s: %v", op, err)
	}
	if op == "CreateTool" {
		var tool tooling.Tool
		if json.Unmarshal(out.Json, &tool) == nil {
			testdb.DisableOnCleanup(t, db, tool.ToolID)
		}
	}
	return out.Json
}
func decode[T any](t *testing.T, raw []byte) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}
func fixture(t *testing.T, db *store.Store, timeout int) (tooling.Tool, tooling.ToolVersion) {
	tool := decode[tooling.Tool](t, api(t, db, "CreateTool", "", rpc.NewID(), tooling.ToolCreate{DisplayName: "A03 database test"}))
	v := decode[tooling.ToolVersion](t, api(t, db, "CreateToolVersion", tool.ToolID, rpc.NewID(), tooling.VersionCreate{ExpectedRevision: 1, Kind: "diagnostic_echo_v1", Binding: tooling.Binding{Prefix: "fixed:"}, TimeoutMS: timeout}))
	return tool, decode[tooling.ToolVersion](t, api(t, db, "PublishToolVersion", v.ToolVersionID, rpc.NewID(), tooling.Revision{ExpectedRevision: 1}))
}
func TestToolsDatabase(t *testing.T) {
	db := testdb.Open(t)
	ctx := testContext()
	if err := db.Migrate(ctx); err != nil {
		t.Fatal(err)
	}
	t.Run("idempotent_management_and_revision", func(t *testing.T) {
		key := rpc.NewID()
		body := tooling.ToolCreate{DisplayName: "concurrent"}
		ids := make(chan string, 8)
		var group sync.WaitGroup
		for range 8 {
			group.Go(func() {
				out, err := db.ToolAPI(ctx, "CreateTool", &platformv1.ToolRequest{IdempotencyKey: key, Json: tooling.JSON(body)})
				if err != nil {
					t.Error(err)
					return
				}
				var tool tooling.Tool
				if json.Unmarshal(out.Json, &tool) != nil {
					t.Error("invalid response")
					return
				}
				ids <- tool.ToolID
			})
		}
		group.Wait()
		close(ids)
		id := ""
		count := 0
		for got := range ids {
			count++
			if id == "" {
				id = got
			}
			if id != got {
				t.Fatal("duplicate resource")
			}
		}
		if count != 8 {
			t.Fatal(count)
		}
		_, err := db.ToolAPI(ctx, "CreateTool", &platformv1.ToolRequest{IdempotencyKey: key, Json: tooling.JSON(tooling.ToolCreate{DisplayName: "changed"})})
		if tooling.Code(err) != "KEY_CONFLICT" {
			t.Fatal(err)
		}
		enabled := false
		api(t, db, "UpdateTool", id, "", tooling.ToolPatch{ExpectedRevision: 1, Enabled: &enabled})
		_, err = db.ToolAPI(ctx, "UpdateTool", &platformv1.ToolRequest{ResourceId: id, Json: tooling.JSON(tooling.ToolPatch{ExpectedRevision: 1, Enabled: &enabled})})
		if tooling.Code(err) != "REVISION_CONFLICT" {
			t.Fatal(err)
		}
	})
	t.Run("accepted_snapshot_replay_disable_and_fencing", func(t *testing.T) {
		tool, v := fixture(t, db, 30000)
		key := rpc.NewID()
		body := tooling.CallCreate{ToolVersionID: v.ToolVersionID, Arguments: tooling.JSON(map[string]string{"text": "hello"}), Mode: "run"}
		first := api(t, db, "CreateToolCall", "", key, body)
		a := decode[tooling.Accepted](t, first)
		enabled := false
		api(t, db, "UpdateTool", tool.ToolID, "", tooling.ToolPatch{ExpectedRevision: 2, Enabled: &enabled})
		second := api(t, db, "CreateToolCall", "", key, body)
		var x, y any
		_ = json.Unmarshal(first, &x)
		_ = json.Unmarshal(second, &y)
		if string(tooling.JSON(x)) != string(tooling.JSON(y)) {
			t.Fatal("replay not initial acceptance")
		}
		_, err := db.ToolAPI(ctx, "CreateToolCall", &platformv1.ToolRequest{IdempotencyKey: rpc.NewID(), Json: tooling.JSON(body)})
		if tooling.Code(err) != "TOOL_VERSION_UNAVAILABLE" {
			t.Fatal(err)
		}
		changed := body
		changed.Arguments = tooling.JSON(map[string]string{"text": "different"})
		_, err = db.ToolAPI(ctx, "CreateToolCall", &platformv1.ToolRequest{IdempotencyKey: key, Json: tooling.JSON(changed)})
		if tooling.Code(err) != "KEY_CONFLICT" {
			t.Fatal(err)
		}
		job, err := db.ClaimTool(ctx)
		if err != nil || job == nil || job.ID != a.ToolCallID || job.Snapshot.Version.DefinitionSHA256 != v.DefinitionSHA256 {
			t.Fatalf("snapshot: %v %v", job, err)
		}
		stale := *job
		stale.LeaseToken = rpc.NewID()
		if tooling.Code(db.FinishTool(ctx, &stale, []byte(`{"data":{"text":"wrong"}}`), "")) != "TOOL_EXECUTION_FENCED" {
			t.Fatal("stale owner persisted")
		}
		if err = db.FinishTool(ctx, job, []byte(`{"data":{"text":"fixed:hello"},"completion":"completed"}`), ""); err != nil {
			t.Fatal(err)
		}
		c, err := db.ToolCall(ctx, a.ToolCallID)
		if err != nil || c.State != "succeeded" || c.StartedAt == nil || c.CompletedAt == nil || c.DefinitionSHA256 != v.DefinitionSHA256 {
			t.Fatalf("call %v %v", c, err)
		}
		if tooling.Code(db.FinishTool(ctx, job, nil, "FAILED")) != "TOOL_EXECUTION_FENCED" {
			t.Fatal("terminal result overwritten")
		}
	})
	t.Run("expired_running_is_not_reexecuted", func(t *testing.T) {
		_, v := fixture(t, db, 30000)
		a := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", rpc.NewID(), tooling.CallCreate{ToolVersionID: v.ToolVersionID, Arguments: []byte(`{"text":"once"}`), Mode: "run"}))
		job, err := db.ClaimTool(ctx)
		if err != nil || job == nil {
			t.Fatal(err)
		}
		if _, err = db.Pool.Exec(ctx, "UPDATE agent.tool_calls SET lease_until=clock_timestamp()-interval '1 second' WHERE tool_call_id=$1", a.ToolCallID); err != nil {
			t.Fatal(err)
		}
		next, err := db.ClaimTool(ctx)
		if err != nil || next != nil {
			t.Fatalf("reclaimed running job: %v %v", next, err)
		}
		c, err := db.ToolCall(ctx, a.ToolCallID)
		if err != nil || c.State != "interrupted" || c.Error.Code != "TOOL_EXECUTION_INTERRUPTED" {
			t.Fatalf("%v %v", c, err)
		}
		if tooling.Code(db.FinishTool(ctx, job, []byte(`{}`), "")) != "TOOL_EXECUTION_FENCED" {
			t.Fatal("late report accepted")
		}
	})
	t.Run("expired_accepted_and_immutable_connection", func(t *testing.T) {
		_, v := fixture(t, db, 30000)
		a := decode[tooling.Accepted](t, api(t, db, "CreateToolCall", "", rpc.NewID(), tooling.CallCreate{ToolVersionID: v.ToolVersionID, Arguments: []byte(`{"text":"queued"}`), Mode: "run"}))
		if _, err := db.Pool.Exec(ctx, "UPDATE agent.tool_calls SET deadline_at=clock_timestamp()-interval '1 second' WHERE tool_call_id=$1", a.ToolCallID); err != nil {
			t.Fatal(err)
		}
		if job, err := db.ClaimTool(ctx); err != nil || job != nil {
			t.Fatalf("%v %v", job, err)
		}
		call, err := db.ToolCall(ctx, a.ToolCallID)
		if err != nil || call.State != "failed" || call.Error.Code != "TOOL_TIMEOUT" {
			t.Fatalf("%v %v", call, err)
		}
		c := decode[tooling.Connection](t, api(t, db, "CreateConnection", "", rpc.NewID(), tooling.ConnectionCreate{DisplayName: "explicit API", ConnectionSpec: tooling.ConnectionSpec{BaseURL: "http://model-gateway:8000", AuthType: "none"}}))
		api(t, db, "CreateConnectionVersion", c.ConnectionID, rpc.NewID(), tooling.ConnectionVersionCreate{ExpectedRevision: 1, ConnectionSpec: tooling.ConnectionSpec{BaseURL: "http://knowledge-gateway:8000", AuthType: "none"}})
		old := decode[tooling.ConnectionVersion](t, api(t, db, "GetConnectionVersion", c.CurrentVersionID, "", nil))
		if old.BaseURL != "http://model-gateway:8000" || old.Version != 1 {
			t.Fatal("connection version changed")
		}
	})
}
