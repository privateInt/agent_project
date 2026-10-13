package main

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	ocrv1 "data-platform/gen/ocr/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/canonical"
	"data-platform/internal/config"
	"data-platform/internal/ocr"
	"data-platform/internal/operationreport"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"io"
	"log"
	"os"
	"path/filepath"
	"time"
)

func main() {
	if len(os.Args) == 2 && os.Args[1] == "--self-test" {
		ctx, cancel := context.WithTimeout(context.Background(), 45*time.Second)
		defer cancel()
		if e := runtimeSelfTest(ctx, parserPython, parserScript); e != nil {
			log.Fatal(e)
		}
		return
	}
	if len(os.Args) != 1 {
		log.Fatal("unsupported job argument")
	}
	if e := run(); e != nil {
		log.Fatal(e)
	}
}
func run() error {
	started := time.Now()
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Minute)
	defer cancel()
	opID, runID := config.Required("OPERATION_ID"), config.Required("RESOURCE_ID")
	defer observeStep(opID, "job_process", started)
	conn, e := grpc.NewClient(config.Required("OPERATION_GRPC_ADDRESS"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fmt.Errorf("operation connection unavailable")
	}
	defer conn.Close()
	reporter := operationreport.New(operationv1.NewOperationServiceClient(conn))
	// No source/domain reads, parsing or writes precede confirmed authorization.
	if e = timedStep(opID, "authorization", func() error { return reporter.Authorize(ctx, opID, runID, "refinement.parse") }); e != nil {
		return fmt.Errorf("execution gate denied or unconfirmed; no refinement started")
	}
	phase := func(p string) {
		c, stop := context.WithTimeout(ctx, 3*time.Second)
		defer stop()
		if _, err := reporter.UpdatePhase(c, &operationv1.UpdateOperationRequest{OperationId: opID, Phase: p}); err != nil {
			log.Printf("phase report unavailable phase=%s", p)
		} else {
			log.Printf("phase=%s", p)
		}
	}
	fail := func(code, message string) error {
		c, stop := context.WithTimeout(context.Background(), 20*time.Second)
		defer stop()
		if _, err := reporter.FailOperation(c, &operationv1.FailOperationRequest{OperationId: opID, Phase: "failed", ErrorCode: code, ErrorMessage: message, Retryable: false}); err != nil {
			return fmt.Errorf("%s; failure report unconfirmed", code)
		}
		return fmt.Errorf("%s: %s", code, message)
	}
	refConn, e := grpc.NewClient(config.Required("REFINEMENT_GRPC_ADDRESS"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fail("REFINEMENT_INPUT_UNAVAILABLE", "Refinement connection unavailable")
	}
	defer refConn.Close()
	ref := refinementv1.NewRefinementServiceClient(refConn)
	c, stop := context.WithTimeout(ctx, 10*time.Second)
	run, e := ref.GetRun(c, &refinementv1.GetRunRequest{RefinementRunId: runID})
	stop()
	if e != nil || run.GetOperationId() != opID {
		return fail("REFINEMENT_INPUT_UNAVAILABLE", "Refinement request identity unavailable")
	}
	colConn, e := grpc.NewClient(config.Required("COLLECTION_GRPC_ADDRESS"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		return fail("REFINEMENT_INPUT_UNAVAILABLE", "Collection connection unavailable")
	}
	defer colConn.Close()
	col := collectionv1.NewCollectionServiceClient(colConn)
	c, stop = context.WithTimeout(ctx, 10*time.Second)
	source, e := col.GetDocument(c, &collectionv1.GetDocumentRequest{DocumentId: run.SourceDocumentId})
	stop()
	if e != nil || source.GetDocumentId() != run.SourceDocumentId || source.GetSha256() != run.SourceSha256 || source.GetState() != "ready" || source.GetSizeBytes() < 1 || source.GetSizeBytes() > canonical.MaxBytes {
		return fail("REFINEMENT_SOURCE_MISMATCH", "Immutable source metadata mismatch")
	}
	format, e := canonical.Format(source)
	if e != nil || format != run.SourceFormat {
		return fail("REFINEMENT_SOURCE_MISMATCH", "Source format mismatch")
	}
	dir, e := os.MkdirTemp("", "refinement-")
	if e != nil {
		return fail("REFINEMENT_LOCAL_IO_FAILED", "Temporary directory unavailable")
	}
	defer os.RemoveAll(dir)
	input, output, specPath := filepath.Join(dir, "source"), filepath.Join(dir, "canonical.json"), filepath.Join(dir, "spec.json")
	phase("reading_source")
	if e = timedStep(opID, "source_download", func() error { return download(ctx, col, source, input) }); e != nil {
		return fail("REFINEMENT_SOURCE_MISMATCH", "Source download or integrity verification failed")
	}
	spec, e := canonical.ExpectedSpec(run.SourceDocumentId, run.SourceSha256, run.SourceFormat, run.RulesVersion, config.Required("PROCESSOR_IMAGE_ID"), run.OcrJson)
	if e != nil {
		return fail("REFINEMENT_OCR_IDENTITY_INVALID", "Stored OCR identity invalid")
	}
	spec.MeasureTimings = true
	if spec.OCR != nil {
		modelConn, err := grpc.NewClient(config.Env("MODEL_DEPLOYMENT_GRPC_ADDRESS", "model-deployment:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
		if err != nil {
			return fail("REFINEMENT_OCR_UNAVAILABLE", "Model discovery unavailable")
		}
		c, stop := context.WithTimeout(ctx, 10*time.Second)
		current, err := ocr.Resolve(c, ocrv1.NewOCRDeploymentServiceClient(modelConn), spec.OCR.DeploymentID)
		stop()
		modelConn.Close()
		if err != nil || current == nil || *current != *spec.OCR {
			return fail("REFINEMENT_OCR_UNAVAILABLE", "Pinned OCR deployment is unavailable or changed")
		}
	}
	raw, e := json.Marshal(spec)
	if e != nil {
		return fail("REFINEMENT_LOCAL_IO_FAILED", "Parser specification unavailable")
	}
	if e = os.WriteFile(specPath, raw, 0600); e != nil {
		return fail("REFINEMENT_LOCAL_IO_FAILED", "Parser specification could not be saved")
	}
	phase("parsing_and_cleaning")
	parseLimit := 7 * time.Minute
	if spec.OCR != nil {
		phase("parsing_and_ocr")
		parseLimit = 17 * time.Minute
	}
	parseCtx, parseCancel := context.WithTimeout(ctx, parseLimit)
	e = timedStep(opID, "parser_process", func() error { return invokeParser(parseCtx, parserPython, parserScript, input, specPath, output) })
	parseCancel()
	if e != nil {
		log.Printf("parser_process_failed %v", e)
		return fail("REFINEMENT_PARSE_FAILED", "Parser execution failed: "+e.Error())
	}
	f, e := os.Open(output)
	if e != nil {
		return fail("REFINEMENT_PARSE_FAILED", "Canonical output unavailable")
	}
	raw, e = io.ReadAll(io.LimitReader(f, canonical.MaxBytes+1))
	f.Close()
	if e != nil || int64(len(raw)) > canonical.MaxBytes {
		return fail("REFINEMENT_PARSE_FAILED", "Canonical output exceeds limits")
	}
	if e = timedStep(opID, "canonical_validation", func() error { _, err := canonical.Validate(raw, spec); return err }); e != nil {
		return fail("REFINEMENT_PARSE_FAILED", "Canonical output provenance validation failed")
	}
	sum := sha256.Sum256(raw)
	digest := hex.EncodeToString(sum[:])
	size := int64(len(raw))
	phase("uploading_canonical")
	uploadStarted := time.Now()
	ioCtx, ioCancel := context.WithTimeout(ctx, 90*time.Second)
	store, e := blob.ConnectCanonical(ioCtx, config.Required("MINIO_ENDPOINT"), config.Required("MINIO_ACCESS_KEY"), config.Required("MINIO_SECRET_KEY"), config.Bool("MINIO_USE_SSL", false))
	if e != nil {
		ioCancel()
		return fail("REFINEMENT_STORAGE_FAILED", "Canonical storage unavailable")
	}
	key, _ := canonical.Key(runID, digest)
	f, e = os.Open(output)
	if e != nil {
		ioCancel()
		return fail("REFINEMENT_LOCAL_IO_FAILED", "Canonical output unavailable")
	}
	e = store.Put(ioCtx, key, f, size, "application/json")
	observeStep(opID, "canonical_upload", uploadStarted)
	f.Close()
	ioCancel()
	if e != nil {
		return fail("REFINEMENT_STORAGE_FAILED", "Canonical upload unconfirmed; no registration was attempted")
	}
	phase("verifying_canonical")
	token, instance := reporter.ExecutionIdentity()
	req := &refinementv1.CompleteRunRequest{RefinementRunId: runID, OperationId: opID, DispatchToken: token, ExecutionInstanceId: instance, Sha256: digest, SizeBytes: size, ProcessorImageId: spec.ImageID}
	var completed *refinementv1.RefinementRun
	registerStarted := time.Now()
	for attempt := 0; attempt < 3; attempt++ {
		c, stop := context.WithTimeout(ctx, 60*time.Second)
		completed, e = ref.CompleteRun(c, req)
		stop()
		if e == nil || ctx.Err() != nil {
			break
		}
	}
	observeStep(opID, "canonical_registration", registerStarted)
	// A lost registration reply is not evidence of failure. Never overwrite an
	// already committed domain result with failed; Reconciler observes it later.
	if e != nil || completed.GetOutputDocumentId() == "" {
		return fmt.Errorf("canonical registration unconfirmed; retained result requires reconciliation")
	}
	phase("canonical_registered")
	c, stop = context.WithTimeout(context.Background(), 20*time.Second)
	defer stop()
	if _, e = reporter.SucceedOperation(c, &operationv1.UpdateOperationRequest{OperationId: opID, Phase: "completed"}); e != nil {
		return fmt.Errorf("canonical registered; completion report requires reconciliation")
	}
	log.Printf("completed refinement_run_id=%s document_id=%s bytes=%d sha256=%s", runID, completed.OutputDocumentId, size, digest)
	return nil
}

// Fixed stage names and numeric durations only: never log source/model output.
func observeStep(operationID, stage string, started time.Time) {
	log.Printf("data_job_timing_v1 operation_id=%s stage=%s elapsed_ms=%d", operationID, stage, time.Since(started).Milliseconds())
}
func timedStep(operationID, stage string, call func() error) error {
	defer observeStep(operationID, stage, time.Now())
	return call()
}
func download(ctx context.Context, col collectionv1.CollectionServiceClient, source *collectionv1.SourceDocument, path string) error {
	c, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	stream, e := col.ReadDocument(c, &collectionv1.GetDocumentRequest{DocumentId: source.DocumentId})
	if e != nil {
		return e
	}
	f, e := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	if e != nil {
		return e
	}
	defer f.Close()
	hash := sha256.New()
	writer := io.MultiWriter(f, hash)
	var n int64
	for {
		chunk, e := stream.Recv()
		if e == io.EOF {
			break
		}
		if e != nil {
			return e
		}
		data := chunk.GetData()
		if len(data) == 0 || len(data) > 64<<10 || int64(len(data)) > source.SizeBytes-n {
			return fmt.Errorf("source stream limit exceeded")
		}
		count, e := writer.Write(data)
		if e != nil {
			return e
		}
		n += int64(count)
	}
	if n != source.SizeBytes || hex.EncodeToString(hash.Sum(nil)) != source.Sha256 {
		return fmt.Errorf("source checksum mismatch")
	}
	return f.Sync()
}
