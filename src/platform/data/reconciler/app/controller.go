package app

import (
	"context"
	"data-platform/dispatcher/executor"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/canonical"
	"data-platform/internal/crawl"
	"data-platform/internal/execution"
	"fmt"
	"log"
	"strings"
	"time"
)

type Observer interface {
	Inspect(context.Context, string, string, string) (*executor.Runtime, error)
}
type Controller struct {
	Refinement refinementv1.RefinementServiceClient
	Operations operationv1.OperationServiceClient
	Collection collectionv1.CollectionServiceClient
	Docker     Observer
}

func (c Controller) Run(ctx context.Context) {
	ticker := time.NewTicker(15 * time.Second)
	defer ticker.Stop()
	for {
		c.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
func (c Controller) Tick(ctx context.Context) {
	q, cancel := context.WithTimeout(ctx, 10*time.Second)
	list, err := c.Operations.ListReconcileOperations(q, &operationv1.ListReconcileOperationsRequest{Limit: 50})
	cancel()
	if err != nil {
		log.Printf("reconciliation list unavailable")
		return
	}
	for _, op := range list.Operations {
		q, cancel := context.WithTimeout(ctx, 20*time.Second)
		err = c.Reconcile(q, op)
		cancel()
		if err != nil {
			log.Printf("reconciliation operation=%s: %v", op.OperationId, err)
		}
	}
}
func (c Controller) Reconcile(ctx context.Context, op *operationv1.Operation) error {
	note := func(target, message string) error {
		_, e := c.Operations.ApplyReconciliation(ctx, &operationv1.ApplyReconciliationRequest{OperationId: op.OperationId, ExpectedStatus: op.Status, ExpectedAttempt: op.Attempt, ExpectedUpdatedAt: op.UpdatedAt, TargetStatus: target, Note: message})
		if e == nil {
			log.Printf("reconciliation operation=%s target=%q note=%s", op.OperationId, target, message)
		}
		return e
	}
	if !execution.Supported(op.GetExecutionType(), op.GetResourceType()) || op.GetDispatchProtocol() != 1 {
		return note("", "manual review: unexpected execution contract")
	}
	if op.GetExecutionStartedAt() == nil {
		if op.GetStatus() != "dispatching" || op.GetDispatchLeaseUntil() == nil || time.Now().Before(op.DispatchLeaseUntil.AsTime()) {
			return note("", "waiting for current launch lease")
		}
		if op.GetDispatchGeneration() >= 3 {
			return note("", "manual review: pre-start recovery budget exhausted")
		}
		_, err := c.Operations.RecoverDispatch(ctx, &operationv1.RecoverDispatchRequest{OperationId: op.OperationId, ExpectedDispatchToken: op.DispatchToken})
		return err
	}
	if op.GetExecutionType() == "refinement.parse" {
		if c.Refinement == nil {
			return fmt.Errorf("refinement observation unavailable")
		}
		run, err := c.Refinement.GetRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: op.ResourceId})
		if err != nil {
			return fmt.Errorf("refinement observation unavailable")
		}
		if run.RefinementRunId != op.ResourceId || run.OperationId != op.OperationId {
			return note("", "manual review: refinement identity mismatch")
		}
		if run.OutputDocumentId != "" {
			doc, err := c.Refinement.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: run.OutputDocumentId})
			if err != nil {
				return fmt.Errorf("canonical observation unavailable")
			}
			if doc.DocumentId != run.OutputDocumentId || doc.RefinementRunId != run.RefinementRunId || doc.SourceDocumentId != run.SourceDocumentId || !canonical.ValidState(doc.State) || doc.SchemaVersion != canonical.Schema || doc.ProcessorVersion != canonical.Processor || doc.RulesVersion != run.RulesVersion || !blob.ValidDigest(doc.Sha256) || doc.SizeBytes < 1 || doc.SizeBytes > canonical.MaxBytes {
				return note("", "manual review: canonical registration mismatch")
			}
			return note("succeeded", "canonical was verified and registered by the authorized process; quality state is independent")
		}
	} else {
		run, err := c.Collection.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: op.ResourceId})
		if err != nil {
			return fmt.Errorf("domain observation unavailable")
		}
		if run.OperationId != op.OperationId || crawl.OptionsFromProto(run.Options).ExecutionType() != op.ExecutionType {
			return note("", "manual review: crawl identity mismatch")
		}
		if run.OutputDocumentId != "" {
			ids := run.GetOutputDocumentIds()
			options := crawl.OptionsFromProto(run.Options)
			if !options.Expanded() && len(ids) == 0 {
				ids = []string{run.OutputDocumentId}
			}
			if len(ids) == 0 || ids[0] != run.OutputDocumentId || (options.Expanded() && len(ids) > int(options.MaxDocuments)) {
				return note("", "manual review: source manifest mismatch")
			}
			seen := map[string]bool{}
			var total int64
			image := ""
			for _, id := range ids {
				if seen[id] {
					return note("", "manual review: duplicate source manifest")
				}
				seen[id] = true
				doc, err := c.Collection.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: id})
				if err != nil {
					return fmt.Errorf("registered source observation unavailable")
				}
				if doc.DocumentId != id || doc.State != "ready" || doc.CrawlRunId != run.CrawlRunId || !blob.ValidDigest(doc.Sha256) || doc.SizeBytes < 0 || doc.SizeBytes > options.MaxBytes {
					return note("", "manual review: source registration mismatch")
				}
				if options.Expanded() {
					if image == "" {
						image = doc.ProcessorImageId
					}
					if doc.ProcessorVersion != options.BatchProcessor() || doc.ProcessorImageId != image || !strings.HasPrefix(image, "sha256:") || !blob.ValidDigest(strings.TrimPrefix(image, "sha256:")) {
						return note("", "manual review: source processor mismatch")
					}
				}
				if crawl.ValidateCapture(options, doc.CrawlRole, doc.ContentType, doc.BrowserCapture) != nil {
					return note("", "manual review: browser response provenance mismatch")
				}
				if doc.BrowserCapture != nil {
					total += doc.BrowserCapture.ResponseSizeBytes
				}
				total += doc.SizeBytes
			}
			if options.Expanded() && total > options.MaxTotalBytes {
				return note("", "manual review: source total size mismatch")
			}

			return note("succeeded", "source was verified and registered by the authorized process")
		}
	}
	runtime, err := c.Docker.Inspect(ctx, op.OperationId, op.DispatchToken, op.ResourceId)
	if err != nil {
		return note("", "manual review: runtime observation unavailable; no automatic re-execution")
	}
	if actual := runtime.Config.Labels["data-platform.execution_type"]; actual != "" && actual != op.ExecutionType {
		return note("", "manual review: runtime execution type mismatch")
	}
	if runtime.State.Running {
		return note("", "authorized CPU process remains running")
	}
	// Exited + no result does not disprove a delayed in-flight registration.
	// The Job reports known failures; never invent a failure after a lost reply.
	return note("", "manual review: exited process has no confirmed result; no automatic re-execution")
}
