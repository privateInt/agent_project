// Package testdb is used only by tests. Production always calls store.Connect.
package testdb

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"os"
	"testing"
	"time"
)

func Open(t *testing.T) *store.Store {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	var db *store.Store
	var err error
	if os.Getenv("AGENT_TEST_DATABASE") == "1" {
		db, err = store.Connect(ctx)
	} else if target := os.Getenv("AGENT_TEST_PGLITE_URL"); target != "" {
		// Supplementary SQL/driver verification on a scratch-only WASM engine.
		// This does not claim deployment identity/permissions or native DB behavior.
		cfg, e := pgxpool.ParseConfig(target)
		if e != nil {
			t.Fatal(e)
		}
		cfg.MaxConns = 1
		cfg.ConnConfig.DefaultQueryExecMode = pgx.QueryExecModeDescribeExec
		pool, e := pgxpool.NewWithConfig(ctx, cfg)
		if e != nil {
			t.Fatal(e)
		}
		db = &store.Store{Pool: pool}
		err = db.Migrate(ctx)
		t.Log("SQL integration uses PGlite; native PostgreSQL acceptance remains separate")
	} else {
		t.Skip("requires dedicated Agent DB checks profile or explicit scratch PGlite URL")
	}
	if err != nil {
		t.Fatal(err)
	}
	db.ToolLane = "test-" + rpc.NewID()
	t.Cleanup(db.Close)
	return db
}

// Keep test definitions and evidence, but do not leave runnable Tools in MCP lists.
func DisableOnCleanup(t *testing.T, db *store.Store, id string) {
	t.Helper()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		out, err := db.ToolAPI(ctx, "GetTool", &platformv1.ToolRequest{ResourceId: id})
		if err != nil {
			t.Error(err)
			return
		}
		var tool tooling.Tool
		if err = json.Unmarshal(out.Json, &tool); err != nil {
			t.Error(err)
			return
		}
		if !tool.Enabled {
			return
		}
		disabled := false
		_, err = db.ToolAPI(ctx, "UpdateTool", &platformv1.ToolRequest{ResourceId: id, Json: tooling.JSON(tooling.ToolPatch{ExpectedRevision: tool.Revision, Enabled: &disabled})})
		if err != nil {
			t.Error(err)
		}
	})
}
