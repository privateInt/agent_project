package runtime

import (
	"context"
	"github.com/google/uuid"
	"os"
	"path/filepath"
	"testing"
)

func TestCachePurgeRejectsSymlinksAndPreservesOtherDeployments(t *testing.T) {
	root := t.TempDir()
	outside := t.TempDir()
	id := uuid.NewString()
	other := uuid.NewString()
	f := SnapshotFetcher{Root: root}
	if e := os.WriteFile(filepath.Join(outside, "keep"), []byte("weights"), 0600); e != nil {
		t.Fatal(e)
	}
	if e := os.Symlink(outside, filepath.Join(root, id)); e != nil {
		t.Fatal(e)
	}
	if e := f.Purge(context.Background(), id); e == nil {
		t.Fatal("symlink accepted")
	}
	if _, e := os.Stat(filepath.Join(outside, "keep")); e != nil {
		t.Fatal(e)
	}
	_ = os.Remove(filepath.Join(root, id))
	_ = os.Mkdir(filepath.Join(root, id), 0700)
	_ = os.Mkdir(filepath.Join(root, other), 0700)
	if e := f.Purge(context.Background(), id); e != nil {
		t.Fatal(e)
	}
	if _, e := os.Stat(filepath.Join(root, other)); e != nil {
		t.Fatal(e)
	}
	if e := f.Purge(context.Background(), "../all"); e == nil {
		t.Fatal("arbitrary directory accepted")
	}
}
