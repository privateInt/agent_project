package main

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/google/uuid"
	serving "model-platform/deployment/runtime"
)

func TestProductionFetcherPurgeDoesNotConnectToMinIO(t *testing.T) {
	root := t.TempDir()
	id, other := uuid.NewString(), uuid.NewString()
	for _, name := range []string{id, other} {
		if err := os.Mkdir(filepath.Join(root, name), 0700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(root, name, "weights"), []byte("model"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	calls := 0
	offline := errors.New("MinIO unavailable")
	// Use the constructor wired by main.go, through the same interface stored
	// by ConfigureServing. A Prepare-only wrapper loses Purge at this boundary.
	var configured serving.Fetcher = newServingFetcher(root, func(context.Context) (serving.ArtifactStore, error) {
		calls++
		return nil, offline
	})
	purger, ok := configured.(interface {
		Purge(context.Context, string) error
	})
	if !ok {
		t.Fatal("production fetcher lost its cache cleanup capability")
	}
	for i := 0; i < 2; i++ {
		if err := purger.Purge(context.Background(), id); err != nil {
			t.Fatal("purge or repeated purge failed", err)
		}
	}
	if calls != 0 {
		t.Fatal("local cache removal connected to MinIO")
	}
	if _, err := os.Stat(filepath.Join(root, id)); !os.IsNotExist(err) {
		t.Fatal("selected cache remains", err)
	}
	if _, err := os.Stat(filepath.Join(root, other, "weights")); err != nil {
		t.Fatal("unrelated deployment cache changed", err)
	}
	if err := configured.Prepare(context.Background(), serving.Plan{}); !errors.Is(err, offline) || calls != 1 {
		t.Fatal("preparation no longer uses the lazy connection", err, calls)
	}
}

func TestProductionFetcherPurgeKeepsPathGuards(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	id := uuid.NewString()
	if err := os.WriteFile(filepath.Join(outside, "keep"), []byte("model"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(outside, filepath.Join(root, id)); err != nil {
		t.Fatal(err)
	}
	f := newServingFetcher(root, func(context.Context) (serving.ArtifactStore, error) {
		t.Fatal("purge must not connect to MinIO")
		return nil, nil
	})
	for _, target := range []string{id, "../all", uuid.Nil.String()} {
		if err := f.Purge(context.Background(), target); err == nil {
			t.Fatal("unsafe cache deletion accepted", target)
		}
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := f.Purge(ctx, uuid.NewString()); !errors.Is(err, context.Canceled) {
		t.Fatal("cancelled purge accepted", err)
	}
	if _, err := os.Stat(filepath.Join(outside, "keep")); err != nil {
		t.Fatal("outside file changed", err)
	}
}
