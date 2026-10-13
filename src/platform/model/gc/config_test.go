package main

import (
	"bytes"
	"context"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestInvalidGCConfigurationFailsClosed(t *testing.T) {
	for _, name := range []string{"GC_MODE", "GC_SUCCESS_RETENTION", "GC_FAILURE_RETENTION", "GC_INTERVAL", "GC_ITEM_TIMEOUT", "GC_MAX_LOG_BYTES", "GC_BATCH_SIZE", "MINIO_USE_SSL"} {
		t.Setenv(name, "")
	}
	c, err := loadSettings()
	if err != nil || c.policy.Mode != "dry-run" || c.policy.SuccessKeep != 24*time.Hour || c.policy.FailureKeep != 168*time.Hour { t.Fatalf("unsafe defaults: %+v %v", c, err) }
	for _, tc := range []struct{ key, value string }{
		{"GC_MODE", "delete"}, {"GC_SUCCESS_RETENTION", "0s"}, {"GC_FAILURE_RETENTION", "-1h"},
		{"GC_INTERVAL", "invalid"}, {"GC_MAX_LOG_BYTES", "0"}, {"GC_MAX_LOG_BYTES", "1073741825"},
		{"GC_BATCH_SIZE", "0"}, {"MINIO_USE_SSL", "invalid"},
	} {
		t.Run(tc.key+"="+tc.value, func(t *testing.T) {
			t.Setenv(tc.key, tc.value)
			if _, err := loadSettings(); err == nil { t.Fatal("invalid setting accepted") }
		})
	}
}

func TestLogBudgetIsSharedAcrossStreams(t *testing.T) {
	var stdout, stderr bytes.Buffer
	budget := &byteBudget{remaining: 31}
	var wg sync.WaitGroup
	for _, out := range []*bytes.Buffer{&stdout, &stderr} {
		wg.Add(1)
		go func(b *bytes.Buffer) {
			defer wg.Done()
			w := limitedWriter{b, budget}
			for i := 0; i < 32; i++ { _, _ = w.Write([]byte("x")) }
		}(out)
	}
	wg.Wait()
	if stdout.Len()+stderr.Len() != 31 || budget.remaining != 0 { t.Fatal("combined byte budget exceeded") }
}

func TestDockerCLIPreservesStreamsAndUsesExactNonForcedRemoval(t *testing.T) {
	dir := t.TempDir()
	argsPath := filepath.Join(dir, "args")
	// This is a fake executable, not a live Docker daemon.
	script := `#!/bin/sh
case "$1" in
logs) printf 'container stdout\n'; printf 'container stderr\n' >&2 ;;
rm) printf '%s\n' "$@" > "$GC_TEST_ARGS" ;;
*) exit 2 ;;
esac
`
	if err := os.WriteFile(filepath.Join(dir, "docker"), []byte(script), 0700); err != nil { t.Fatal(err) }
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("GC_TEST_ARGS", argsPath)
	id := strings.Repeat("a", 64)
	var stdout, stderr bytes.Buffer
	d := dockerCLI{}
	if err := d.Logs(context.Background(), id, &stdout, &stderr); err != nil { t.Fatal(err) }
	if stdout.String() != "container stdout\n" || stderr.String() != "container stderr\n" { t.Fatal("log streams were mixed or dropped") }
	if err := d.Remove(context.Background(), id); err != nil { t.Fatal(err) }
	args, err := os.ReadFile(argsPath)
	if err != nil || string(args) != "rm\n"+id+"\n" { t.Fatalf("unsafe removal arguments: %q %v", args, err) }
	if err := d.Remove(context.Background(), "--force"); err == nil { t.Fatal("non-ID removal argument accepted") }
}
