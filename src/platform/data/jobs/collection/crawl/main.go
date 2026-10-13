package main

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/blob"
	"data-platform/internal/config"
	"data-platform/internal/crawl"
	"data-platform/internal/operationreport"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"os"
	"time"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--browser-self-test" {
		if e := browserSelfTest(); e != nil {
			log.Fatal(e)
		}
		return
	}
	if e := run(); e != nil {
		log.Fatal(e)
	}
}
func run() error {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Minute)
	defer cancel()
	opID, runID := config.Required("OPERATION_ID"), config.Required("RESOURCE_ID")
	conn, e := grpc.NewClient(config.Required("OPERATION_GRPC_ADDRESS"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return e
	}
	defer conn.Close()
	reporter := operationreport.New(operationv1.NewOperationServiceClient(conn))
	// No domain reads, downloads or writes may happen before this grant.
	kind := config.Required("EXECUTION_TYPE")
	if kind != "collection.crawl" && kind != "collection.browser" {
		return fmt.Errorf("unsupported collection job")
	}
	if e = reporter.Authorize(ctx, opID, runID, kind); e != nil {
		return fmt.Errorf("execution gate denied or unconfirmed; no collection started")
	}
	phase := func(p string) {
		c, stop := context.WithTimeout(ctx, 3*time.Second)
		defer stop()
		_, err := reporter.UpdatePhase(c, &operationv1.UpdateOperationRequest{OperationId: opID, Phase: p})
		if err != nil {
			log.Printf("phase report unavailable phase=%s", p)
		} else {
			log.Printf("phase=%s", p)
		}
	}
	fail := func(code, message string) error {
		c, stop := context.WithTimeout(context.Background(), 20*time.Second)
		defer stop()
		_, err := reporter.FailOperation(c, &operationv1.FailOperationRequest{OperationId: opID, Phase: "failed", ErrorCode: code, ErrorMessage: message, Retryable: false})
		if err != nil {
			return fmt.Errorf("%s; failure report unconfirmed", code)
		}
		return fmt.Errorf("%s: %s", code, message)
	}
	cc, e := grpc.NewClient(config.Required("COLLECTION_GRPC_ADDRESS"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fail("CRAWL_INPUT_UNAVAILABLE", "Collection service connection failed")
	}
	defer cc.Close()
	collection := collectionv1.NewCollectionServiceClient(cc)
	inputCtx, stop := context.WithTimeout(ctx, 10*time.Second)
	run, e := collection.GetCrawl(inputCtx, &collectionv1.GetCrawlRequest{CrawlRunId: runID})
	stop()
	if e != nil || run.GetOperationId() != opID {
		return fail("CRAWL_INPUT_UNAVAILABLE", "Crawl request could not be verified")
	}
	if crawl.OptionsFromProto(run.Options).ExecutionType() != kind {
		return fail("CRAWL_INPUT_UNAVAILABLE", "Execution type differs from saved request")
	}
	if crawl.OptionsFromProto(run.Options).Expanded() {
		return runExpanded(ctx, run, reporter, collection, phase, fail)
	}
	phase("downloading_source")
	result, e := crawl.NewFetcher().Fetch(ctx, run.Url, crawl.Options{MaxBytes: run.Options.GetMaxBytes(), TimeoutSeconds: run.Options.GetTimeoutSeconds()})
	if e != nil {
		return fail("CRAWL_FETCH_FAILED", crawl.PublicError(e))
	}
	defer os.Remove(result.Path)
	phase("uploading_source")
	ioCtx, ioCancel := context.WithTimeout(ctx, 90*time.Second)
	store, e := blob.Connect(ioCtx, config.Required("MINIO_ENDPOINT"), config.Required("MINIO_ACCESS_KEY"), config.Required("MINIO_SECRET_KEY"), "data-sources", config.Bool("MINIO_USE_SSL", false))
	if e != nil {
		ioCancel()
		return fail("CRAWL_STORAGE_FAILED", "Source storage is unavailable")
	}
	key, _ := blob.Key(runID, result.SHA256)
	file, e := os.Open(result.Path)
	if e != nil {
		ioCancel()
		return fail("CRAWL_STORAGE_FAILED", "Downloaded source is unavailable")
	}
	e = store.Put(ioCtx, key, file, result.Size, result.ContentType)
	file.Close()
	ioCancel()
	if e != nil {
		return fail("CRAWL_STORAGE_FAILED", "Source upload did not complete; no document registered")
	}
	phase("verifying_source")
	token, instance := reporter.ExecutionIdentity()
	request := &collectionv1.CompleteCrawlRequest{CrawlRunId: runID, OperationId: opID, DispatchToken: token, ExecutionInstanceId: instance, Sha256: result.SHA256, SizeBytes: result.Size, ContentType: result.ContentType, FinalUrl: result.FinalURL, HttpStatus: int32(result.Status), ProcessorImageId: config.Required("PROCESSOR_IMAGE_ID")}
	// Retry the exact result registration, never repeat the HTTP download.
	// A lost commit reply is not proof of failure: leave lifecycle reconciliation
	// to the retained, hash-verified result rather than failing a successful crawl.
	var completed *collectionv1.CrawlRun
	for attempt := 0; attempt < 3; attempt++ {
		c, stop := context.WithTimeout(ctx, 60*time.Second)
		completed, e = collection.CompleteCrawl(c, request)
		stop()
		if e == nil {
			break
		}
		if ctx.Err() != nil {
			break
		}
	}
	if e != nil || completed.GetOutputDocumentId() == "" {
		return fmt.Errorf("source result registration unconfirmed; retained object and execution require reconciliation")
	}
	phase("source_registered")
	c, finish := context.WithTimeout(context.Background(), 20*time.Second)
	defer finish()
	if _, e = reporter.SucceedOperation(c, &operationv1.UpdateOperationRequest{OperationId: opID, Phase: "completed"}); e != nil {
		return fmt.Errorf("source registered; completion report requires reconciliation")
	}
	log.Printf("completed crawl_run_id=%s document_id=%s bytes=%d sha256=%s", runID, completed.OutputDocumentId, result.Size, result.SHA256)
	return nil
}
