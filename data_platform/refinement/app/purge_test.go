package app

import (
	"context"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"errors"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"strings"
	"testing"
	"time"
)

type purgeCanonicalStore struct {
	*memoryStore
	deletes int
	fail    bool
}

func (m *purgeCanonicalStore) PurgeRun(context.Context, string) error {
	m.deletes++
	if m.fail {
		return errors.New("offline")
	}
	m.body = ""
	return nil
}
func TestPurgeResultPreservesSourceAndFencesNewWork(t *testing.T) {
	s, ops, m, _ := fixture(t)
	ctx := context.Background()
	r := prepare(t, s, ops, m)
	run, e := s.CompleteRun(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	store := &purgeCanonicalStore{memoryStore: m}
	s.store = store
	target := &refinementv1.GetRunRequest{RefinementRunId: r.RefinementRunId}
	if _, e = s.PurgeRun(ctx, target); status.Code(e) != codes.FailedPrecondition || store.deletes != 0 {
		t.Fatal("running result purged", e)
	}
	if _, e = ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId, Phase: "completed"}); e != nil {
		t.Fatal(e)
	}
	source := &refinementv1.GetDocumentRequest{DocumentId: run.SourceDocumentId}
	if _, e = s.BlockSourcePurge(ctx, source); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("live result lost protection", e)
	}
	store.fail = true
	if _, e = s.PurgeRun(ctx, target); status.Code(e) != codes.Unavailable {
		t.Fatal(e)
	}
	if _, e = s.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: run.OutputDocumentId}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("pending purge downloaded", e)
	}
	store.fail = false
	done, e := s.PurgeRun(ctx, target)
	if e != nil || done.PurgedAt == nil || m.body != "" {
		t.Fatal(done, e)
	}
	if _, e = s.CompleteRun(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("completion resurrected files", e)
	}
	if _, e = s.BlockSourcePurge(ctx, source); e != nil {
		t.Fatal(e)
	}
	if _, e = s.CreateRun(ctx, &refinementv1.CreateRunRequest{Name: "new", DocumentId: source.DocumentId, IdempotencyKey: uuid.NewString()}); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("new request raced purge fence", e)
	}
	if _, e = s.PurgeRun(ctx, target); e != nil || store.deletes != 2 {
		t.Fatal("purge replay rewrote storage", e, store.deletes)
	}
}

func TestSourcePurgeWaitsForAnUncommittedRefinement(t *testing.T) {
	s, _, _, _ := fixture(t)
	ctx := context.Background()
	id := uuid.NewString()
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		t.Fatal(e)
	}
	defer tx.Rollback(ctx)
	if e = sourcePurgeLock(ctx, tx, id); e != nil {
		t.Fatal(e)
	}
	if _, e = tx.Exec(ctx, `INSERT INTO refinement_runs(refinement_run_id,operation_id,name,source_document_id,source_sha256,source_format,rules_version) VALUES($1,$2,'pending',$3,$4,'text','basic-cleaning-v1')`, uuid.NewString(), uuid.NewString(), id, strings.Repeat("a", 64)); e != nil {
		t.Fatal(e)
	}
	done := make(chan error, 1)
	go func() { _, e := s.BlockSourcePurge(ctx, &refinementv1.GetDocumentRequest{DocumentId: id}); done <- e }()
	select {
	case e := <-done:
		t.Fatal("purge did not wait for creation", e)
	case <-time.After(30 * time.Millisecond):
	}
	if e = tx.Commit(ctx); e != nil {
		t.Fatal(e)
	}
	select {
	case e := <-done:
		if status.Code(e) != codes.FailedPrecondition {
			t.Fatal("in-flight reference missed", e)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("fence stayed blocked")
	}
}
