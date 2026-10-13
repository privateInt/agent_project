package app

import (
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	ocrv1 "data-platform/gen/ocr/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/ocr"
	"encoding/hex"
	"encoding/json"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"
)

type imageCollection struct {
	collectionv1.CollectionServiceClient
}

func (*imageCollection) GetDocument(_ context.Context, r *collectionv1.GetDocumentRequest, _ ...grpc.CallOption) (*collectionv1.SourceDocument, error) {
	return &collectionv1.SourceDocument{DocumentId: r.DocumentId, State: "ready", Sha256: strings.Repeat("a", 64), SizeBytes: 100, Format: "png"}, nil
}

type ocrDiscovery struct {
	ocrv1.OCRDeploymentServiceClient
	calls   int
	offline bool
}

func (d *ocrDiscovery) ResolveDeployment(_ context.Context, r *ocrv1.ResolveDeploymentRequest, _ ...grpc.CallOption) (*ocrv1.DeploymentIdentity, error) {
	d.calls++
	if d.offline {
		return nil, status.Error(codes.Unavailable, "offline")
	}
	return &ocrv1.DeploymentIdentity{DeploymentId: r.DeploymentId, ModelVersionId: "22222222-2222-4222-8222-222222222222", RuntimeImageId: "sha256:" + strings.Repeat("b", 64), RuntimeContainerId: strings.Repeat("c", 64), InternalEndpoint: "http://model-serving-" + r.DeploymentId + ":8000/v1", ServedModelName: "deployment-" + r.DeploymentId, Profile: ocr.Profile, SourceManifestSha256: strings.Repeat("d", 64)}, nil
}

func TestOCRRequestPinsIdentityAndReplaySurvivesModelOutage(t *testing.T) {
	s, _, _, _ := fixture(t)
	s.collection = &imageCollection{}
	resolver := &ocrDiscovery{}
	s.ConfigureOCR(resolver)
	input := request()
	input.OcrDeploymentId = uuid.NewString()
	ctx := context.Background()
	if _, e := s.CreateRun(ctx, input); status.Code(e) != codes.InvalidArgument {
		t.Fatal("legacy RPC accepted OCR")
	}
	first, e := s.CreateOCRRun(ctx, input)
	if e != nil {
		t.Fatal(e)
	}
	run, e := s.GetRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: first.RefinementRunId})
	if e != nil {
		t.Fatal(e)
	}
	pinned, e := ocr.Decode(run.OcrJson)
	if e != nil || pinned.DeploymentID != input.OcrDeploymentId {
		t.Fatal(pinned, e)
	}
	resolver.offline = true
	replay, e := s.CreateOCRRun(ctx, input)
	if e != nil || replay.RefinementRunId != first.RefinementRunId || resolver.calls != 1 {
		t.Fatal("replay re-resolved model identity", e)
	}
	changed := proto.Clone(input).(*refinementv1.CreateRunRequest)
	changed.OcrDeploymentId = uuid.NewString()
	if _, e = s.CreateOCRRun(ctx, changed); status.Code(e) != codes.AlreadyExists {
		t.Fatal("changed OCR key did not conflict", e)
	}
	changed.IdempotencyKey = uuid.NewString()
	if _, e = s.CreateOCRRun(ctx, changed); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("unavailable model accepted", e)
	}
	var count int
	if e = s.pool.QueryRow(ctx, `SELECT count(*) FROM refinement_runs`).Scan(&count); e != nil || count != 1 {
		t.Fatal("partial OCR request persisted", count, e)
	}
}

func TestOCRCompletionKeepsModelProvenanceAndProcessorVersion(t *testing.T) {
	s, ops, store, _ := fixture(t)
	ctx := context.Background()
	req := prepare(t, s, ops, store)
	identity, e := ocr.Resolve(ctx, &ocrDiscovery{}, uuid.NewString())
	if e != nil {
		t.Fatal(e)
	}
	encoded, _ := json.Marshal(identity)
	if _, e = s.pool.Exec(ctx, `UPDATE refinement_runs SET ocr_json=$2,source_format='png' WHERE refinement_run_id=$1`, req.RefinementRunId, string(encoded)); e != nil {
		t.Fatal(e)
	}
	var body map[string]any
	if e = json.Unmarshal([]byte(store.body), &body); e != nil {
		t.Fatal(e)
	}
	body["source"].(map[string]any)["format"] = "png"
	body["processor"].(map[string]any)["version"] = ocr.Processor
	body["pages"] = []any{map[string]any{"number": 1}}
	body["quality"] = map[string]any{"state": "needs_review", "issues": []map[string]any{{"code": "ocr_output_requires_review", "requires_ocr": false}}}
	result := map[string]any{"identity": identity, "client_version": ocr.ClientVersion, "policy": "selective-pages-v1", "pages": []map[string]any{{"page": 1, "status": "completed", "reason": "", "native_blocks": []any{}}}}
	body["ocr"] = result
	assign := func() {
		raw, _ := json.Marshal(body)
		store.body = string(raw)
		sum := sha256.Sum256(raw)
		req.Sha256 = hex.EncodeToString(sum[:])
		req.SizeBytes = int64(len(raw))
	}
	bad := *identity
	bad.RuntimeImageID = "sha256:" + strings.Repeat("f", 64)
	result["identity"] = &bad
	assign()
	if _, e = s.CompleteRun(ctx, req); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("wrong OCR model registered", e)
	}
	result["identity"] = identity
	assign()
	completed, e := s.CompleteRun(ctx, req)
	if e != nil {
		t.Fatal(e)
	}
	document, e := s.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: completed.OutputDocumentId})
	if e != nil || document.ProcessorVersion != ocr.Processor || document.State != "needs_review" {
		t.Fatal("OCR metadata lost", document, e)
	}
}
