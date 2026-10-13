package main

import (
	"context"
	"fmt"
	"log"
	"os"
	"time"

	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/blob"
	"data-platform/internal/browser"
	"data-platform/internal/config"
	"data-platform/internal/crawl"
	"data-platform/internal/operationreport"
)

func runExpanded(ctx context.Context, run *collectionv1.CrawlRun, reporter *operationreport.Client, collection collectionv1.CollectionServiceClient, phase func(string), fail func(string, string) error) error {
	ioCtx, stop := context.WithTimeout(ctx, 30*time.Second)
	store, e := blob.Connect(ioCtx, config.Required("MINIO_ENDPOINT"), config.Required("MINIO_ACCESS_KEY"), config.Required("MINIO_SECRET_KEY"), "data-sources", config.Bool("MINIO_USE_SSL", false))
	stop()
	if e != nil {
		return fail("CRAWL_STORAGE_FAILED", "Source storage is unavailable")
	}
	phase("collecting_sources")
	var render crawl.RenderFunc
	if run.Options.GetRenderJavascript() {
		phase("rendering_pages")
		render = browser.New().Render
	}
	storageFailed := false
	batch, e := crawl.NewFetcher().CollectRendered(ctx, run.Url, crawl.OptionsFromProto(run.Options), func(c context.Context, item crawl.Item) error {
		if item.Response != nil {
			raw := item.Response
			key, e := blob.BrowserResponseKey(run.CrawlRunId, item.SourceURL, raw.SHA256)
			if e != nil {
				return e
			}
			file, e := os.Open(raw.Path)
			if e == nil {
				e = store.Put(c, key, file, raw.Size, raw.ContentType)
				file.Close()
			}
			if e != nil {
				storageFailed = true
				return e
			}
		}
		key, e := blob.CrawlItemKey(run.CrawlRunId, item.SourceURL, item.SHA256)
		if e != nil {
			return e
		}
		file, e := os.Open(item.Path)
		if e != nil {
			storageFailed = true
			return e
		}
		defer file.Close()
		ioCtx, stop := context.WithTimeout(c, 60*time.Second)
		defer stop()
		if e = store.Put(ioCtx, key, file, item.Size, item.ContentType); e != nil {
			storageFailed = true
			return e
		}
		log.Printf("source_stored depth=%d role=%s bytes=%d sha256=%s", item.Depth, item.Role, item.Size, item.SHA256)
		return nil
	}, render)
	if e != nil {
		if storageFailed {
			return fail("CRAWL_STORAGE_FAILED", "Batch upload failed; no source documents registered")
		}
		if run.Options.GetRenderJavascript() {
			return fail("BROWSER_CAPTURE_FAILED", "Browser capture failed: resource access, rendering, or configured limit; no documents registered")
		}
		return fail("CRAWL_FETCH_FAILED", crawl.PublicError(e))
	}
	phase("verifying_sources")
	token, instance := reporter.ExecutionIdentity()
	request := &collectionv1.CompleteCrawlBatchRequest{CrawlRunId: run.CrawlRunId, OperationId: run.OperationId, DispatchToken: token, ExecutionInstanceId: instance, ProcessorImageId: config.Required("PROCESSOR_IMAGE_ID"), DocumentLimitReached: batch.DocumentLimitReached, DepthLimitReached: batch.DepthLimitReached, LinkLimitReached: batch.LinkLimitReached}
	for _, item := range batch.Items {
		request.Sources = append(request.Sources, &collectionv1.CrawlSourceResult{SourceUrl: item.SourceURL, FinalUrl: item.FinalURL, ParentUrl: item.ParentURL, Depth: item.Depth, Role: item.Role, Sha256: item.SHA256, SizeBytes: item.Size, ContentType: item.ContentType, HttpStatus: int32(item.Status), BrowserCapture: item.Capture})
	}
	var completed *collectionv1.CrawlRun
	for attempt := 0; attempt < 3; attempt++ {
		c, stop := context.WithTimeout(ctx, 90*time.Second)
		completed, e = collection.CompleteCrawlBatch(c, request)
		stop()
		if e == nil || ctx.Err() != nil {
			break
		}
	}
	if e != nil || len(completed.GetOutputDocumentIds()) != len(batch.Items) {
		return fmt.Errorf("batch registration unconfirmed; retained objects require reconciliation; no automatic download retry")
	}
	phase("sources_registered")
	c, finish := context.WithTimeout(context.Background(), 20*time.Second)
	defer finish()
	if _, e = reporter.SucceedOperation(c, &operationv1.UpdateOperationRequest{OperationId: run.OperationId, Phase: "completed"}); e != nil {
		return fmt.Errorf("sources registered; completion report requires reconciliation")
	}
	log.Printf("completed crawl_run_id=%s documents=%d document_limit=%t depth_limit=%t link_limit=%t", run.CrawlRunId, len(batch.Items), batch.DocumentLimitReached, batch.DepthLimitReached, batch.LinkLimitReached)
	return nil
}
