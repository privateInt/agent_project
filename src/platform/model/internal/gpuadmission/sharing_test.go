package gpuadmission

import (
	"context"
	"testing"
)

func TestServingSharesOccupiedGPUButJobsRemainExclusive(t *testing.T) {
	ctx := context.Background()
	first := req("first", 0)
	second := req("second", 0)
	second.Fenced = false
	second.Shared = true
	var items []Container
	g := Guard{Dir: t.TempDir(), Inventory: func(context.Context) ([]Container, error) { return items, nil }}
	if e := g.Run(ctx, first, func() error { return nil }); e != nil {
		t.Fatal(e)
	}
	items = []Container{active("another-model", "0")}
	called := false
	if e := g.Run(ctx, second, func() error { called = true; return nil }); e != nil || !called {
		t.Fatal("shared model blocked", e)
	}
	if e := g.Run(ctx, req("training", 0), func() error { t.Fatal("exclusive job launched"); return nil }); e == nil {
		t.Fatal("exclusive guard bypassed")
	}
	// A pre-upgrade serving claim can be resumed under the new sharing policy.
	legacy := req("legacy", 1)
	legacy.Fenced = false
	if e := g.Run(ctx, legacy, func() error { return nil }); e != nil {
		t.Fatal(e)
	}
	legacy.Shared = true
	if e := g.Run(ctx, legacy, func() error { return nil }); e != nil {
		t.Fatal("old claim cannot resume", e)
	}
}
