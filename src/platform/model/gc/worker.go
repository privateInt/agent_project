package main

import (
	"context"
	"fmt"
	"sort"
	"time"

	operationv1 "model-platform/gen/operation/v1"
)

type getOperation func(context.Context, string) (*operationv1.Operation, error)

type cachedArchive struct {
	snapshot Snapshot
	receipt  Receipt
}

type Result struct {
	ContainerID string `json:"container_id"`
	OperationID string `json:"operation_id,omitempty"`
	Status      string `json:"status,omitempty"`
	Action      string `json:"action"`
	ArchiveURI  string `json:"archive_uri,omitempty"`
	Note        string `json:"note,omitempty"`
}

type worker struct {
	policy   Policy
	docker   Docker
	get      getOperation
	store    ObjectStore
	maxBytes int64
	now      func() time.Time
	cache    map[string]cachedArchive
	cursor   string
}

func (w *worker) snapshot(ctx context.Context, id string) (Snapshot, error) {
	c, err := w.docker.Inspect(ctx, id)
	if err != nil {
		return Snapshot{}, err
	}
	operationID := c.Labels[labelPrefix+"operation_id"]
	if c.ID != id || !uuidRE.MatchString(operationID) {
		return Snapshot{}, fmt.Errorf("missing or invalid container identity")
	}
	rpcCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	op, err := w.get(rpcCtx, operationID)
	if err != nil {
		return Snapshot{}, fmt.Errorf("operation lookup unavailable or missing; retained")
	}
	return w.policy.candidate(c, op, w.now())
}

func (w *worker) process(ctx context.Context, id string) (Result, error) {
	r := Result{ContainerID: id, Action: "retained"}
	if err := ctx.Err(); err != nil {
		return r, err
	}
	s, err := w.snapshot(ctx, id)
	if err != nil {
		r.Note = err.Error()
		delete(w.cache, id)
		// Lookup failure is deliberately visible but cannot authorize collection.
		return r, nil
	}
	r.OperationID, r.Status = s.OperationID, s.Status
	due := w.policy.removable(s, w.now())
	if w.policy.Mode == "dry-run" {
		r.Action = "would_archive"
		if due {
			r.Action = "would_archive_and_remove"
		}
		return r, nil
	}
	if w.policy.Mode != "archive" && w.policy.Mode != "apply" {
		return r, fmt.Errorf("invalid GC mode")
	}
	if w.store == nil {
		return r, fmt.Errorf("archive store is unavailable")
	}
	if w.cache == nil {
		w.cache = make(map[string]cachedArchive)
	}
	cached, ok := w.cache[id]
	if !ok || cached.snapshot != s {
		receipt, err := archive(ctx, w.docker, w.store, s, w.maxBytes)
		if err != nil {
			return r, fmt.Errorf("archive incomplete; container retained: %w", err)
		}
		current, err := w.snapshot(ctx, id)
		if err != nil || current != s {
			delete(w.cache, id)
			return r, fmt.Errorf("container or operation changed during archive; retained")
		}
		cached = cachedArchive{snapshot: s, receipt: receipt}
		w.cache[id] = cached
	}
	r.ArchiveURI = w.store.URI(cached.receipt.Prefix)
	r.Action = "archived_retained"
	if w.policy.Mode == "archive" || !due {
		return r, nil
	}
	// Recheck the remote bytes even if this process already archived the Job.
	// An archive cache hit alone is never permission to remove a container.
	if err := cached.receipt.Verify(ctx, w.store); err != nil {
		delete(w.cache, id)
		return r, fmt.Errorf("archive verification failed; retained: %w", err)
	}
	current, err := w.snapshot(ctx, id)
	if err != nil || current != s || !w.policy.removable(current, w.now()) {
		delete(w.cache, id)
		return r, fmt.Errorf("final ownership, state or retention check changed; retained")
	}
	if err := ctx.Err(); err != nil {
		return r, err
	}
	if err := w.docker.Remove(ctx, id); err != nil {
		return r, fmt.Errorf("container removal not confirmed: %w", err)
	}
	delete(w.cache, id)
	r.Action = "removed"
	return r, nil
}

// Round-robin by immutable ID prevents recent/unsupported containers from
// starving older jobs when a cycle is capped. Cache is bounded by live IDs.
func (w *worker) batch(ids []string, limit int) []string {
	sort.Strings(ids)
	present := make(map[string]bool, len(ids))
	for _, id := range ids {
		present[id] = true
	}
	for id := range w.cache {
		if !present[id] {
			delete(w.cache, id)
		}
	}
	if len(ids) == 0 || limit <= 0 {
		return nil
	}
	start := sort.Search(len(ids), func(i int) bool { return ids[i] > w.cursor })
	if limit > len(ids) {
		limit = len(ids)
	}
	selected := make([]string, 0, limit)
	for i := 0; i < limit; i++ {
		selected = append(selected, ids[(start+i)%len(ids)])
	}
	w.cursor = selected[len(selected)-1]
	return selected
}
