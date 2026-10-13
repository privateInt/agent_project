package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	commonv1 "model-platform/gen/common/v1"
	operationv1 "model-platform/gen/operation/v1"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

var testNow = time.Date(2026, 9, 8, 12, 0, 0, 0, time.UTC)

func fixture() (Inspect, *operationv1.Operation, Policy) {
	op := &operationv1.Operation{
		OperationId: "11111111-1111-4111-8111-111111111111", DispatchToken: "22222222-2222-4222-8222-222222222222",
		ExecutionInstanceId: "33333333-3333-4333-8333-333333333333", ResourceId: "44444444-4444-4444-8444-444444444444",
		DispatchProtocol: 1, ExecutionType: "deployment.apply", ResourceType: "deployment", Status: "succeeded",
		ExecutionStartedAt: timestamppb.New(testNow.Add(-49*time.Hour)), CompletedAt: timestamppb.New(testNow.Add(-48*time.Hour)),
		Resources: &commonv1.ExecutionResources{},
	}
	c := Inspect{
		ID: strings.Repeat("a", 64), Name: "/model-op-"+op.OperationId+"-"+op.DispatchToken,
		Image: "sha256:"+strings.Repeat("b", 64), RestartPolicy: "no", LogDriver: "json-file",
		Labels: map[string]string{labelPrefix+"owner": "operation-dispatcher", labelPrefix+"operation_id": op.OperationId,
			labelPrefix+"dispatch_token": op.DispatchToken, labelPrefix+"execution_type": op.ExecutionType},
		Networks: map[string]json.RawMessage{"ops-infra": json.RawMessage(`{}`)},
	}
	c.State.Status = "exited"
	c.State.StartedAt, c.State.FinishedAt = testNow.Add(-49*time.Hour), testNow.Add(-48*time.Hour)
	return c, op, Policy{Mode: "apply", Network: "ops-infra", SuccessKeep: 24*time.Hour, FailureKeep: 168*time.Hour}
}

func TestCollectionPolicy(t *testing.T) {
	cases := []struct { name string; change func(*Inspect, *operationv1.Operation, *Policy) }{
		{"foreign owner", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"owner"] = "other" }},
		{"persistent serving runtime", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"owner"] = "deployment-serving" }},
		{"wrong name", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Name += "-other" }},
		{"stale token", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"dispatch_token"] = "old" }},
		{"different operation", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"operation_id"] = "old" }},
		{"wrong execution", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"execution_type"] = "training.run" }},
		{"test fixture", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Labels[labelPrefix+"smoke"] = "true" }},
		{"legacy", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.DispatchProtocol = 0 }},
		{"ungranted", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.ExecutionStartedAt = nil }},
		{"missing process", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.ExecutionInstanceId = "" }},
		{"nonterminal", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.Status = "running" }},
		{"no completion", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.CompletedAt = nil }},
		{"future completion", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.CompletedAt = timestamppb.New(testNow.Add(time.Hour)) }},
		{"running", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.Running = true }},
		{"paused", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.Paused = true }},
		{"restarting", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.Restarting = true }},
		{"dead", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.Status = "dead" }},
		{"restarted", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.RestartCount = 1 }},
		{"restart policy", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.RestartPolicy = "always" }},
		{"automatic removal", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.AutoRemove = true }},
		{"future finish", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.FinishedAt = testNow.Add(time.Hour) }},
		{"no finish", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.State.FinishedAt = time.Time{} }},
		{"TTY", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.Tty = true }},
		{"no logging", func(c *Inspect, _ *operationv1.Operation, _ *Policy) { c.LogDriver = "none" }},
		{"wrong network", func(_ *Inspect, _ *operationv1.Operation, p *Policy) { p.Network = "other" }},
		{"wrong node", func(c *Inspect, op *operationv1.Operation, _ *Policy) { op.Resources.NodeName = "other"; c.Labels[labelPrefix+"requested_node"] = "other" }},
		{"wrong resource", func(_ *Inspect, op *operationv1.Operation, _ *Policy) { op.ResourceType = "training_run" }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			c, op, p := fixture()
			tc.change(&c, op, &p)
			if _, err := p.candidate(c, op, testNow); err == nil { t.Fatal("unsafe candidate accepted") }
		})
	}
	for execution, resource := range map[string]string{"deployment.apply":"deployment", "deployment.serve":"deployment", "training.run":"training_run", "management.import-huggingface":"model_version"} {
		for _, state := range []string{"succeeded", "failed"} {
			c, op, p := fixture()
			op.ExecutionType, op.ResourceType, op.Status = execution, resource, state
			c.Labels[labelPrefix+"execution_type"] = execution
			if _, err := p.candidate(c, op, testNow); err != nil { t.Fatal(err) }
		}
	}
}

func TestRetentionUsesLaterCompletion(t *testing.T) {
	c, op, p := fixture()
	s, err := p.candidate(c, op, testNow)
	if err != nil { t.Fatal(err) }
	if !p.removable(s, testNow) { t.Fatal("old success should be eligible") }
	s.Status = "failed"
	if p.removable(s, testNow) { t.Fatal("failure needs seven days") }
	s.Status, s.CompletedAt = "succeeded", testNow.Add(-time.Hour)
	if p.removable(s, testNow) { t.Fatal("completion report must also age") }
	s.CompletedAt, s.FinishedAt = testNow.Add(-48*time.Hour), testNow.Add(-time.Hour)
	if p.removable(s, testNow) { t.Fatal("container finish must also age") }
	s.FinishedAt = testNow.Add(-24*time.Hour)
	if !p.removable(s, testNow) { t.Fatal("exact retention boundary should be eligible") }
}

type fakeDocker struct {
	c Inspect
	logs, removes, inspections int
	logErr, removeErr error
	onInspect func(int, *Inspect)
}
func (d *fakeDocker) List(context.Context) ([]string, error) { return []string{d.c.ID}, nil }
func (d *fakeDocker) Inspect(_ context.Context, _ string) (Inspect, error) {
	d.inspections++
	if d.onInspect != nil { d.onInspect(d.inspections, &d.c) }
	return d.c, nil
}
func (d *fakeDocker) Logs(_ context.Context, _ string, out, errOut io.Writer) error {
	d.logs++
	if d.logErr != nil { return d.logErr }
	if _, err := io.WriteString(out, "2026-09-06T12:00:00.000000000Z demo stdout\n"); err != nil { return err }
	_, err := io.WriteString(errOut, "2026-09-06T12:00:00.000000001Z demo stderr\n")
	return err
}
func (d *fakeDocker) Remove(_ context.Context, id string) error {
	if id != d.c.ID { return fmt.Errorf("wrong removal target") }
	d.removes++
	return d.removeErr
}

type memoryStore struct {
	objects map[string][]byte
	puts int
	failPut, corruptSuffix string
	denyGet bool
}
func (s *memoryStore) Get(_ context.Context, key string) (io.ReadCloser, error) {
	if s.denyGet { return nil, fmt.Errorf("access denied") }
	b, ok := s.objects[key]
	if !ok { return nil, os.ErrNotExist }
	if s.corruptSuffix != "" && strings.HasSuffix(key, s.corruptSuffix) { b = []byte("corrupt") }
	return io.NopCloser(bytes.NewReader(b)), nil
}
func (s *memoryStore) Put(_ context.Context, key string, r io.Reader, size int64) error {
	s.puts++
	if s.failPut != "" && strings.HasSuffix(key, s.failPut) { return fmt.Errorf("store offline") }
	b, err := io.ReadAll(r)
	if err != nil { return err }
	if int64(len(b)) != size { return fmt.Errorf("wrong put size") }
	s.objects[key] = b
	return nil
}
func (s *memoryStore) URI(key string) string { return "s3://test-logs/"+key }

func testWorker() (*worker, *fakeDocker, *memoryStore, *operationv1.Operation) {
	c, op, p := fixture()
	d := &fakeDocker{c: c}
	s := &memoryStore{objects: make(map[string][]byte)}
	w := &worker{policy: p, docker: d, store: s, maxBytes: 1<<20, now: func() time.Time { return testNow },
		get: func(context.Context, string) (*operationv1.Operation, error) { return proto.Clone(op).(*operationv1.Operation), nil },
	}
	return w, d, s, op
}

func TestDryRunIsReadOnly(t *testing.T) {
	w, d, store, _ := testWorker()
	w.policy.Mode, w.store = "dry-run", nil
	r, err := w.process(context.Background(), d.c.ID)
	if err != nil || r.Action != "would_archive_and_remove" || d.logs != 0 || d.removes != 0 || store.puts != 0 { t.Fatalf("dry-run side effects: %+v %v", r, err) }
}

func TestArchiveRetryReusesImmutableObjects(t *testing.T) {
	w, d, store, _ := testWorker()
	w.policy.Mode = "archive"
	first, err := w.process(context.Background(), d.c.ID)
	if err != nil || first.Action != "archived_retained" || d.removes != 0 || store.puts != 4 { t.Fatalf("archive failed: %+v %v", first, err) }
	if _, err := w.process(context.Background(), d.c.ID); err != nil { t.Fatal(err) }
	if d.logs != 1 { t.Fatal("young cached archive was needlessly collected again") }
	w.cache = nil // A collector restart loses only its optimization cache.
	again, err := w.process(context.Background(), d.c.ID)
	if err != nil || first.ArchiveURI != again.ArchiveURI || store.puts != 4 { t.Fatalf("retry changed archive: %+v %v", again, err) }
	for key, data := range store.objects {
		if strings.HasSuffix(key, "metadata.json") && bytes.Contains(data, []byte("MINIO_SECRET_KEY")) { t.Fatal("metadata leaked environment") }
	}
}

func TestApplyArchivesBeforeRemoval(t *testing.T) {
	w, d, store, _ := testWorker()
	d.onInspect = func(n int, _ *Inspect) {
		if n >= 2 && len(store.objects) != 4 { t.Error("final inspection happened before archive completion") }
	}
	r, err := w.process(context.Background(), d.c.ID)
	if err != nil || r.Action != "removed" || d.removes != 1 || d.inspections != 3 { t.Fatalf("apply: %+v %v", r, err) }
}

func TestArchiveFailuresNeverRemove(t *testing.T) {
	for _, failure := range []string{"logs", "overflow", "upload", "readback", "manifest", "manifest readback", "access denied", "lookup", "cancelled"} {
		t.Run(failure, func(t *testing.T) {
			w, d, store, _ := testWorker()
			ctx := context.Background()
			switch failure {
			case "logs": d.logErr = fmt.Errorf("logs unavailable")
			case "overflow": w.maxBytes = 8
			case "upload": store.failPut = "stdout.log"
			case "readback": store.corruptSuffix = "stderr.log"
			case "manifest": store.failPut = "manifest.json"
			case "manifest readback": store.corruptSuffix = "manifest.json"
			case "access denied": store.denyGet = true
			case "lookup": w.get = func(context.Context, string) (*operationv1.Operation, error) { return nil, fmt.Errorf("offline") }
			case "cancelled": var cancel context.CancelFunc; ctx, cancel = context.WithCancel(ctx); cancel()
			}
			r, err := w.process(ctx, d.c.ID)
			if d.removes != 0 { t.Fatal("removed without confirmed archive") }
			if failure != "lookup" && err == nil { t.Fatalf("failure hidden: %+v", r) }
			if failure == "lookup" && r.Action != "retained" { t.Fatal("lookup failure was not retained") }
		})
	}
}

func TestStateChangesBlockRemoval(t *testing.T) {
	for _, change := range []string{"running", "token", "finish", "operation", "restart"} {
		t.Run(change, func(t *testing.T) {
			w, d, _, op := testWorker()
			d.onInspect = func(n int, c *Inspect) {
				if n != 3 { return }
				switch change {
				case "running": c.State.Running = true
				case "token": op.DispatchToken = "55555555-5555-4555-8555-555555555555"
				case "finish": c.State.FinishedAt = c.State.FinishedAt.Add(time.Second)
				case "operation": op.Status = "running"
				case "restart": c.RestartCount++
				}
			}
			if _, err := w.process(context.Background(), d.c.ID); err == nil || d.removes != 0 { t.Fatal("changed state was collected") }
		})
	}
}

func TestCachedArchiveMustStillExistBeforeRemoval(t *testing.T) {
	w, d, store, _ := testWorker()
	w.policy.Mode = "archive"
	if _, err := w.process(context.Background(), d.c.ID); err != nil { t.Fatal(err) }
	w.policy.Mode, store.corruptSuffix = "apply", "stdout.log"
	if _, err := w.process(context.Background(), d.c.ID); err == nil || d.removes != 0 { t.Fatal("cache bypassed remote verification") }
}

func TestFailedRemovalCanRetryWithoutRelaunchOrOverwrite(t *testing.T) {
	w, d, store, _ := testWorker()
	d.removeErr = fmt.Errorf("Docker unavailable")
	if _, err := w.process(context.Background(), d.c.ID); err == nil { t.Fatal("remove failure hidden") }
	d.removeErr = nil
	r, err := w.process(context.Background(), d.c.ID)
	if err != nil || r.Action != "removed" || store.puts != 4 || d.logs != 1 { t.Fatalf("retry: %+v %v", r, err) }
}

func TestRoundRobinDoesNotStarveOldCandidates(t *testing.T) {
	w := &worker{cache: map[string]cachedArchive{"gone": {}}}
	seen := map[string]bool{}
	for i := 0; i < 3; i++ {
		for _, id := range w.batch([]string{"a", "b", "c", "d", "e"}, 2) { seen[id] = true }
	}
	if len(seen) != 5 || len(w.cache) != 0 { t.Fatal("batch starvation or stale cache") }
}
