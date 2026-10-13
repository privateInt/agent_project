package main

import (
	"context"
	"io"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/jobcontrol"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Explicit opt-in: one CPU fixture and a UUID bucket, with no production DB,
// Kafka event, model artifact or training request. The live collector cannot
// resolve this random Operation ID in its database and must retain the fixture.
func TestLiveGCArchiveAndRemoval(t *testing.T) {
	liveArchiveAndRemoval(t, false)
}

// Exercise the same reader used by Training against real Docker and MinIO.
// Only the UUID CPU fixture is removed; production Operations stay untouched.
func TestLiveTrainingLogArchiveFallback(t *testing.T) {
	liveArchiveAndRemoval(t, true)
}

func liveArchiveAndRemoval(t *testing.T, trainingLogs bool) {
	t.Helper()
	endpoint := os.Getenv("GC_TEST_MINIO_ENDPOINT")
	if endpoint == "" {
		t.Skip("set GC_TEST_MINIO_ENDPOINT using tests/run_gc_integration.py")
	}
	imageID, network := os.Getenv("GC_TEST_IMAGE"), os.Getenv("GC_TEST_NETWORK")
	if !strings.HasPrefix(imageID, "sha256:") || network == "" {
		t.Fatal("GC_TEST_IMAGE must be a local immutable image ID and GC_TEST_NETWORK is required")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Minute)
	defer cancel()
	useSSL, err := strconv.ParseBool(env("GC_TEST_MINIO_USE_SSL", "false"))
	if err != nil { t.Fatal("invalid GC_TEST_MINIO_USE_SSL") }
	client, err := minio.New(endpoint, &minio.Options{
		Creds: credentials.NewStaticV4(os.Getenv("GC_TEST_MINIO_ACCESS_KEY"), os.Getenv("GC_TEST_MINIO_SECRET_KEY"), ""),
		Secure: useSSL,
	})
	if err != nil { t.Fatal(err) }
	runID := uuid.NewString()
	bucket := "model-gc-test-"+runID
	if exists, err := client.BucketExists(ctx, bucket); err != nil || exists { t.Fatalf("test bucket collision or lookup failure: %v", err) }
	if err := client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil { t.Fatal(err) }
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cleanupCancel()
		for obj := range client.ListObjects(cleanupCtx, bucket, minio.ListObjectsOptions{Recursive: true}) {
			if obj.Err != nil { t.Errorf("test bucket listing failed: %v", obj.Err); return }
			if err := client.RemoveObject(cleanupCtx, bucket, obj.Key, minio.RemoveObjectOptions{}); err != nil { t.Errorf("test object cleanup: %v", err) }
		}
		if err := client.RemoveBucket(cleanupCtx, bucket); err != nil { t.Errorf("test bucket cleanup (%s): %v", bucket, err) }
	})
	_, op, policy := fixture()
	op.OperationId, op.DispatchToken, op.ExecutionInstanceId, op.ResourceId = uuid.NewString(), uuid.NewString(), uuid.NewString(), uuid.NewString()
	op.Status = "failed"
	if trainingLogs {
		op.Status, op.ExecutionType, op.ResourceType = "cancelled", "training.run", "training_run"
	}
	name := "model-op-"+op.OperationId+"-"+op.DispatchToken
	// Cleanup is registered before launch to cover a lost Docker response.
	t.Cleanup(func() {
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 15*time.Second)
		defer cleanupCancel()
		b, err := dockerOutput(cleanupCtx, "ps", "-aq", "--no-trunc", "--filter", "label=model-platform.gc-test="+runID)
		if err != nil { t.Errorf("test container cleanup listing: %v", err); return }
		for _, id := range strings.Fields(string(b)) {
			c, err := (dockerCLI{}).Inspect(cleanupCtx, id)
			if err != nil || c.Name != "/"+name || c.Labels[labelPrefix+"gc-test"] != runID { t.Errorf("test cleanup ownership check failed"); continue }
			// --force is confined to this exact, owned test fixture on test failure.
			if _, err := dockerOutput(cleanupCtx, "rm", "--force", id); err != nil { t.Errorf("test container cleanup: %v", err) }
		}
	})
	b, err := dockerOutput(ctx, "run", "-d", "--pull=never", "--name", name, "--network", network,
		"--log-driver", "json-file", "--label", "model-platform.owner=operation-dispatcher",
		"--label", "model-platform.operation_id="+op.OperationId, "--label", "model-platform.dispatch_token="+op.DispatchToken,
		"--label", "model-platform.execution_type="+op.ExecutionType, "--label", "model-platform.gc-test="+runID,
		imageID, "/bin/sh", "-c", "printf 'gc stdout marker\\n'; printf 'gc stderr marker\\n' >&2; exit 7")
	if err != nil { t.Fatal(err) }
	id := strings.TrimSpace(string(b))
	docker := dockerCLI{}
	var c Inspect
	for {
		c, err = docker.Inspect(ctx, id)
		if err != nil { t.Fatal(err) }
		if c.State.Status == "exited" { break }
		select { case <-ctx.Done(): t.Fatal(ctx.Err()); case <-time.After(100*time.Millisecond): }
	}
	op.ExecutionStartedAt = timestamppb.New(c.State.StartedAt)
	op.CompletedAt = timestamppb.New(c.State.FinishedAt)
	var logReader *jobcontrol.Runtime
	if trainingLogs {
		archiveReader, err := jobcontrol.NewArchive(endpoint, os.Getenv("GC_TEST_MINIO_ACCESS_KEY"), os.Getenv("GC_TEST_MINIO_SECRET_KEY"), bucket, useSSL)
		if err != nil { t.Fatal(err) }
		logReader = jobcontrol.New("", archiveReader)
		checkLiveTrainingLogs(t, ctx, logReader, op, "docker")
	}
	policy.Mode, policy.Network, policy.FailureKeep = "dry-run", network, time.Second
	store := &s3Store{client: client, bucket: bucket}
	w := &worker{policy: policy, docker: docker, store: store, maxBytes: 1<<20,
		now: func() time.Time { return c.State.FinishedAt.Add(2*time.Second) },
		get: func(context.Context, string) (*operationv1.Operation, error) { return op, nil },
	}
	r, err := w.process(ctx, id)
	if err != nil || r.Action != "would_archive_and_remove" { t.Fatalf("dry-run: %+v %v", r, err) }
	for obj := range client.ListObjects(ctx, bucket, minio.ListObjectsOptions{Recursive: true}) {
		if obj.Err != nil { t.Fatal(obj.Err) }
		t.Fatal("dry-run wrote an archive")
	}
	w.policy.Mode = "archive"
	r, err = w.process(ctx, id)
	if err != nil || r.Action != "archived_retained" { t.Fatalf("archive: %+v %v", r, err) }
	if _, err := docker.Inspect(ctx, id); err != nil { t.Fatal("archive mode removed its container") }
	prefix := strings.TrimPrefix(r.ArchiveURI, "s3://"+bucket+"/")
	for file, marker := range map[string]string{"stdout.log":"gc stdout marker", "stderr.log":"gc stderr marker", "metadata.json":`"exit_code":7`} {
		obj, err := store.Get(ctx, prefix+file)
		if err != nil { t.Fatal(err) }
		data, err := io.ReadAll(io.LimitReader(obj, 1<<20))
		obj.Close()
		if err != nil || !strings.Contains(string(data), marker) { t.Fatalf("%s did not preserve expected data", file) }
	}
	w.policy.Mode = "apply"
	r, err = w.process(ctx, id)
	if err != nil || r.Action != "removed" { t.Fatalf("apply: %+v %v", r, err) }
	if _, err := docker.Inspect(ctx, id); err == nil { t.Fatal("collected container still exists") }
	// Logs must remain readable even after the source container is gone.
	obj, err := store.Get(ctx, prefix+"stdout.log")
	if err != nil { t.Fatal(err) }
	data, err := io.ReadAll(obj)
	obj.Close()
	if err != nil || !strings.Contains(string(data), "gc stdout marker") { t.Fatal("archive lost after container removal") }
	if trainingLogs {
		checkLiveTrainingLogs(t, ctx, logReader, op, "archive")
		t.Log("Training log reader: source=docker before collection; source=archive after removal; stdout/stderr and execution identity verified")
	}
	t.Logf("real Docker stdout/stderr -> MinIO readback -> retention -> remove passed; isolated bucket=%s", bucket)
}

func checkLiveTrainingLogs(t *testing.T, ctx context.Context, reader *jobcontrol.Runtime, op *operationv1.Operation, source string) {
	t.Helper()
	logs, err := reader.Logs(ctx, op, 20, 16384)
	if err != nil { t.Fatal("Training log reader failed", err) }
	if logs.GetSource() != source || logs.GetAvailability() != "available" ||
		logs.GetOperationId() != op.GetOperationId() || logs.GetTrainingRunId() != op.GetResourceId() ||
		!strings.Contains(logs.GetStdout(), "gc stdout marker") || !strings.Contains(logs.GetStderr(), "gc stderr marker") ||
		strings.Contains(logs.GetStdout(), "gc stderr marker") || strings.Contains(logs.GetStderr(), "gc stdout marker") || logs.GetTruncated() {
		t.Fatal("Training log reader source, identity or stdout/stderr mismatch")
	}
}
