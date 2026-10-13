package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/canonical"
	"encoding/hex"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"io"
	"net/http"
	"strconv"
	"time"
)

func decodeRefinement(body []byte) (*refinementv1.CreateRunRequest, error) {
	bad := func() (*refinementv1.CreateRunRequest, error) {
		return nil, status.Error(codes.InvalidArgument, "expected name, document_id and optional rules_version, ocr_deployment_id; duplicate, unknown and null fields are not accepted")
	}
	dec := json.NewDecoder(bytes.NewReader(body))
	if uniqueJSON(dec) != nil {
		return bad()
	}
	if _, e := dec.Token(); e != io.EOF {
		return bad()
	}
	var raw map[string]json.RawMessage
	if json.Unmarshal(body, &raw) != nil || raw == nil {
		return bad()
	}
	req := &refinementv1.CreateRunRequest{}
	for key, value := range raw {
		if string(value) == "null" {
			return bad()
		}
		var target *string
		switch key {
		case "name":
			target = &req.Name
		case "document_id":
			target = &req.DocumentId
		case "ocr_deployment_id":
			target = &req.OcrDeploymentId
		case "rules_version":
			target = &req.RulesVersion
		default:
			return bad()
		}
		if json.Unmarshal(value, target) != nil {
			return bad()
		}
	}
	if req.Name == "" || req.DocumentId == "" {
		return bad()
	}
	return req, nil
}
func (s *Server) refinementReady(w http.ResponseWriter) bool {
	if s.Refinement == nil {
		failure(w, status.Error(codes.Unavailable, "Refinement service unavailable"))
		return false
	}
	return true
}
func (s *Server) createRefinement(w http.ResponseWriter, r *http.Request) {
	if !s.refinementReady(w) {
		return
	}
	body, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 16<<10))
	if e != nil {
		failure(w, status.Error(codes.InvalidArgument, "request body exceeds 16 KiB"))
		return
	}
	req, e := decodeRefinement(body)
	if e != nil {
		failure(w, e)
		return
	}
	req.IdempotencyKey = r.Header.Get("Idempotency-Key")
	if len(r.Header.Values("Idempotency-Key")) != 1 || req.IdempotencyKey == "" {
		failure(w, status.Error(codes.InvalidArgument, "one Idempotency-Key header is required"))
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	var accepted *refinementv1.RunAccepted
	if req.OcrDeploymentId != "" {
		accepted, e = s.Refinement.CreateOCRRun(ctx, req)
	} else {
		accepted, e = s.Refinement.CreateRun(ctx, req)
	}
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 202, map[string]any{"refinement_run_id": accepted.RefinementRunId, "operation_id": accepted.OperationId, "status": "accepted"})
}
func refinementView(r *refinementv1.RefinementRun) map[string]any {
	var ocrIdentity any
	if r.OcrJson != "" {
		_ = json.Unmarshal([]byte(r.OcrJson), &ocrIdentity)
	}
	return map[string]any{"purge_state": r.GetPurgeState(), "purged_at": when(r.GetPurgedAt()), "deleted_at": when(r.DeletedAt), "ocr": ocrIdentity, "refinement_run_id": r.RefinementRunId, "operation_id": r.OperationId, "name": r.Name, "source_document_id": r.SourceDocumentId, "source_sha256": r.SourceSha256, "source_format": r.SourceFormat, "rules_version": r.RulesVersion, "output_document_id": r.OutputDocumentId, "created_at": when(r.CreatedAt), "submission_status": r.SubmissionStatus, "submission_error_code": r.SubmissionErrorCode}
}
func canonicalView(d *refinementv1.CanonicalDocument) map[string]any {
	return map[string]any{"document_id": d.DocumentId, "refinement_run_id": d.RefinementRunId, "source_document_id": d.SourceDocumentId, "sha256": d.Sha256, "size_bytes": d.SizeBytes, "artifact_uri": d.ArtifactUri, "state": d.State, "schema_version": d.SchemaVersion, "processor_version": d.ProcessorVersion, "rules_version": d.RulesVersion, "processor_image_id": d.ProcessorImageId, "block_count": d.BlockCount, "page_count": d.PageCount, "created_at": when(d.CreatedAt)}
}
func (s *Server) getRefinement(w http.ResponseWriter, r *http.Request) {
	if !s.refinementReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	run, e := s.Refinement.GetRun(ctx, &refinementv1.GetRunRequest{RefinementRunId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	out := refinementView(run)
	op, e := s.Operations.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: run.OperationId})
	if e != nil && status.Code(e) != codes.NotFound {
		failure(w, e)
		return
	}
	out["status"] = "created"
	out["phase"] = "submission_pending"
	out["error_code"] = ""
	out["error_message"] = ""
	if op != nil {
		out["status"] = op.Status
		out["phase"] = op.Phase
		out["error_code"] = op.ErrorCode
		out["error_message"] = op.ErrorMessage
	}
	write(w, 200, out)
}
func (s *Server) listRefinements(w http.ResponseWriter, r *http.Request) {
	if !s.refinementReady(w) {
		return
	}
	if flag := r.URL.Query().Get("include_deleted"); flag != "" && flag != "true" && flag != "false" {
		failure(w, status.Error(codes.InvalidArgument, "include_deleted must be true or false"))
		return
	}
	limit, e := page(r, "q", "include_deleted")
	if e != nil {
		failure(w, e)
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	list, e := s.Refinement.ListRuns(ctx, &refinementv1.ListRunsRequest{Limit: limit, Cursor: r.URL.Query().Get("cursor"), Q: r.URL.Query().Get("q"), IncludeDeleted: r.URL.Query().Get("include_deleted") == "true", SourceDocumentId: r.URL.Query().Get("source_document_id")})
	if e != nil {
		failure(w, e)
		return
	}
	items := make([]map[string]any, 0, len(list.Items))
	for _, run := range list.Items {
		items = append(items, refinementView(run))
	}
	write(w, 200, map[string]any{"items": items, "next_cursor": list.NextCursor})
}
func (s *Server) getCanonical(w http.ResponseWriter, r *http.Request) {
	if !s.refinementReady(w) {
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	d, e := s.Refinement.GetDocument(ctx, &refinementv1.GetDocumentRequest{DocumentId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, canonicalView(d))
}
func (s *Server) downloadCanonical(w http.ResponseWriter, r *http.Request) {
	if !s.refinementReady(w) {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	select {
	case s.downloads <- struct{}{}:
		defer func() { <-s.downloads }()
	case <-ctx.Done():
		failure(w, status.FromContextError(ctx.Err()).Err())
		return
	}
	req := &refinementv1.GetDocumentRequest{DocumentId: r.PathValue("id")}
	d, e := s.Refinement.GetDocument(ctx, req)
	if e != nil {
		failure(w, e)
		return
	}
	if d.SizeBytes < 1 || d.SizeBytes > canonical.MaxBytes {
		failure(w, status.Error(codes.DataLoss, "invalid canonical size"))
		return
	}
	stream, e := s.Refinement.ReadDocument(ctx, req)
	if e != nil {
		failure(w, e)
		return
	}
	var data bytes.Buffer
	for {
		chunk, err := stream.Recv()
		if err == io.EOF {
			break
		}
		if err != nil {
			failure(w, err)
			return
		}
		b := chunk.GetData()
		if len(b) == 0 || len(b) > 64<<10 || int64(len(b))+int64(data.Len()) > d.SizeBytes {
			failure(w, status.Error(codes.DataLoss, "canonical stream exceeds limits"))
			return
		}
		data.Write(b)
	}
	hash := sha256.Sum256(data.Bytes())
	if int64(data.Len()) != d.SizeBytes || hex.EncodeToString(hash[:]) != d.Sha256 {
		failure(w, status.Error(codes.DataLoss, "canonical checksum mismatch"))
		return
	}
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Content-Disposition", `attachment; filename="canonical.json"`)
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Length", strconv.Itoa(data.Len()))
	w.WriteHeader(200)
	_, _ = w.Write(data.Bytes())
}
