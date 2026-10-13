package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"context"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"os"
	"testing"
	"time"
)

func TestDatabaseFoundation(t *testing.T) {
	if os.Getenv("AGENT_TEST_DATABASE") != "1" {
		t.Skip("requires the dedicated Agent PostgreSQL; use checks profile")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	s, err := Connect(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer s.Close()
	if err = s.Migrate(ctx); err != nil {
		t.Fatalf("repeat migration: %v", err)
	}
	var createDB, publicUsage bool
	if err = s.Pool.QueryRow(ctx, `SELECT has_database_privilege(current_user,current_database(),'CREATE'),has_schema_privilege(current_user,'public','USAGE')`).Scan(&createDB, &publicUsage); err != nil {
		t.Fatal(err)
	}
	if createDB || publicUsage {
		t.Fatal("Agent role has excess privileges")
	}
	now := timestamppb.New(time.Now().UTC().Truncate(time.Microsecond))
	c := &platformv1.SystemCheck{CheckId: rpc.NewID(), RequestId: rpc.NewID(), State: "succeeded", RuntimeInstanceId: rpc.NewID(), CreatedAt: now, CompletedAt: now}
	if err = s.Save(ctx, c); err != nil {
		t.Fatal(err)
	}
	second, err := Connect(ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer second.Close()
	got, err := second.Get(ctx, c.CheckId)
	if err != nil || !proto.Equal(c, got) {
		t.Fatalf("persisted result: %v %v", got, err)
	}
	if err = s.Save(ctx, c); err == nil {
		t.Fatal("duplicate check ID accepted")
	}
	t.Logf("persistent diagnostic retained: %s", c.CheckId)
}
