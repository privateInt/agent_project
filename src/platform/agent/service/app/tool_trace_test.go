package app

import (
	"agent-platform/internal/store"
	"context"
	"testing"

	"google.golang.org/grpc/metadata"
)

func TestToolTraceUsesDurableParentOnly(t *testing.T) {
	run, step := "10000000-0000-4000-8000-000000000001", "search"
	j := &store.ToolJob{RequestID: "10000000-0000-4000-8000-000000000002", ParentAgentRunID: &run, ParentStepID: &step}
	ctx := toolTraceContext(context.Background(), j)
	md, _ := metadata.FromOutgoingContext(ctx)
	if len(md.Get("x-agent-parent-run-id")) != 1 || md.Get("x-agent-parent-run-id")[0] != run || md.Get("x-agent-parent-step-id")[0] != step || md.Get("x-request-id")[0] != j.RequestID {
		t.Fatal("durable Tool parent lost")
	}
	for _, invalid := range []string{"", "caller-input", "00000000-0000-0000-0000-000000000000"} {
		j.ParentAgentRunID = &invalid
		md, _ = metadata.FromOutgoingContext(toolTraceContext(context.Background(), j))
		if len(md.Get("x-agent-parent-run-id")) != 0 {
			t.Fatal("unverified ancestry was propagated")
		}
	}
	j.ParentAgentRunID = nil
	md, _ = metadata.FromOutgoingContext(toolTraceContext(context.Background(), j))
	if len(md.Get("x-agent-parent-step-id")) != 0 {
		t.Fatal("standalone Tool acquired a parent")
	}
}
