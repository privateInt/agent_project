package app

import (
	"bytes"
	"context"
	"crypto/sha256"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/crawl"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
	"io"
	"mime"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type Server struct {
	Refinement refinementv1.RefinementServiceClient
	Collection collectionv1.CollectionServiceClient
	Operations operationv1.OperationServiceClient
	downloads  chan struct{}
	uploads    chan struct{}
}

func New(c collectionv1.CollectionServiceClient, o operationv1.OperationServiceClient) *Server {
	return &Server{Collection: c, Operations: o, downloads: make(chan struct{}, 2), uploads: make(chan struct{}, 2)}
}
func (s *Server) Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("PUT /api/source-documents/{id}/group", s.assignDocumentGroup)
	mux.HandleFunc("POST /api/source-groups", s.createGroup)
	mux.HandleFunc("GET /api/source-groups", s.listGroups)
	mux.HandleFunc("GET /api/source-groups/{id}", s.getGroup)
	mux.HandleFunc("PUT /api/source-groups/{id}", s.renameGroup)
	mux.HandleFunc("PUT /api/source-groups/{id}/documents/{document_id}", s.groupMembership)
	mux.HandleFunc("DELETE /api/source-groups/{id}/documents/{document_id}", s.groupMembership)
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) { write(w, 200, map[string]string{"status": "ok"}) })
	mux.HandleFunc("POST /api/crawl-runs", s.create)
	mux.HandleFunc("GET /api/crawl-runs", s.listCrawls)
	mux.HandleFunc("GET /api/crawl-runs/{id}", s.getCrawl)
	mux.HandleFunc("POST /api/source-documents", s.upload)
	mux.HandleFunc("GET /api/source-documents", s.listDocuments)
	mux.HandleFunc("GET /api/source-documents/{id}", s.getDocument)
	mux.HandleFunc("DELETE /api/source-documents/{id}", s.deleteDocument)
	mux.HandleFunc("POST /api/source-documents/{id}/purge", s.purgeDocument)
	mux.HandleFunc("GET /api/source-documents/{id}/content", s.download)
	mux.HandleFunc("GET /api/source-documents/{id}/http-response", s.download)
	mux.HandleFunc("GET /api/operations", s.listOperations)
	mux.HandleFunc("GET /api/operations/{id}", s.getOperation)
	mux.HandleFunc("POST /api/refinement-runs", s.createRefinement)
	mux.HandleFunc("GET /api/refinement-runs", s.listRefinements)
	mux.HandleFunc("GET /api/refinement-runs/{id}", s.getRefinement)
	mux.HandleFunc("DELETE /api/refinement-runs/{id}", s.deleteRefinement)
	mux.HandleFunc("POST /api/refinement-runs/{id}/purge", s.purgeRefinement)
	mux.HandleFunc("GET /api/canonical-documents/{id}", s.getCanonical)
	mux.HandleFunc("GET /api/canonical-documents/{id}/content", s.downloadCanonical)
	return mux
}
func write(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}
func failure(w http.ResponseWriter, err error) {
	code := status.Code(err)
	httpCode := 500
	message := "Service request failed"
	switch code {
	case codes.InvalidArgument:
		httpCode = 400
	case codes.NotFound:
		httpCode = 404
	case codes.AlreadyExists, codes.FailedPrecondition:
		httpCode = 409
	case codes.ResourceExhausted:
		httpCode = 429
	case codes.Unavailable:
		httpCode = 503
	case codes.DeadlineExceeded:
		httpCode = 504
	case codes.DataLoss:
		httpCode = 502
	}
	if httpCode == 400 || httpCode == 404 || httpCode == 409 {
		message = status.Convert(err).Message()
	}
	write(w, httpCode, map[string]string{"error_code": code.String(), "error_message": message})
}
func when(t *timestamppb.Timestamp) any {
	if t == nil {
		return nil
	}
	return t.AsTime().UTC().Format(time.RFC3339Nano)
}
func operationView(o *operationv1.Operation) map[string]any {
	return map[string]any{"operation_id": o.GetOperationId(), "kind": o.GetKind(), "resource_type": o.GetResourceType(), "resource_id": o.GetResourceId(), "status": o.GetStatus(), "phase": o.GetPhase(), "attempt": o.GetAttempt(), "error_code": o.GetErrorCode(), "error_message": o.GetErrorMessage(), "created_at": when(o.GetCreatedAt()), "updated_at": when(o.GetUpdatedAt()), "completed_at": when(o.GetCompletedAt()), "execution_started_at": when(o.GetExecutionStartedAt())}
}
func crawlView(c *collectionv1.CrawlRun) map[string]any {
	return map[string]any{"crawl_run_id": c.GetCrawlRunId(), "operation_id": c.GetOperationId(), "name": c.GetName(), "url": c.GetUrl(), "options": crawl.OptionsFromProto(c.GetOptions()), "output_document_id": c.GetOutputDocumentId(), "output_document_ids": sourceIDs(c), "document_limit_reached": c.GetDocumentLimitReached(), "depth_limit_reached": c.GetDepthLimitReached(), "link_limit_reached": c.GetLinkLimitReached(), "created_at": when(c.GetCreatedAt()), "submission_status": c.GetSubmissionStatus(), "submission_error_code": c.GetSubmissionErrorCode()}
}
func documentView(d *collectionv1.SourceDocument) map[string]any {
	groups := make([]map[string]any, 0, len(d.GetGroups()))
	for _, g := range d.GetGroups() {
		groups = append(groups, groupView(g))
	}
	return map[string]any{"groups": groups, "purge_state": d.GetPurgeState(), "purged_at": when(d.GetPurgedAt()), "deleted_at": when(d.GetDeletedAt()), "document_id": d.GetDocumentId(), "crawl_run_id": d.GetCrawlRunId(), "name": d.GetName(), "source_url": d.GetSourceUrl(), "final_url": d.GetFinalUrl(), "sha256": d.GetSha256(), "size_bytes": d.GetSizeBytes(), "content_type": d.GetContentType(), "artifact_uri": d.GetArtifactUri(), "state": d.GetState(), "processor_version": d.GetProcessorVersion(), "processor_image_id": d.GetProcessorImageId(), "created_at": when(d.GetCreatedAt()), "source_type": d.GetSourceType(), "original_filename": d.GetOriginalFilename(), "format": d.GetFormat(), "format_version": d.GetFormatVersion(), "parent_url": d.GetParentUrl(), "crawl_depth": d.GetCrawlDepth(), "crawl_role": d.GetCrawlRole(), "browser_capture": browserCaptureView(d.GetBrowserCapture())}
}
func browserCaptureView(c *collectionv1.BrowserCapture) any {
	if c == nil {
		return nil
	}
	return map[string]any{"representation": c.Representation, "response_sha256": c.ResponseSha256, "response_size_bytes": c.ResponseSizeBytes, "response_content_type": c.ResponseContentType, "response_artifact_uri": c.ResponseArtifactUri, "network_requests": c.NetworkRequests, "network_bytes": c.NetworkBytes, "blocked_requests": c.BlockedRequests}
}
func requestContext(r *http.Request) (context.Context, context.CancelFunc) {
	return context.WithTimeout(r.Context(), 20*time.Second)
}

// A token walk rejects duplicate keys before JSON's last-key-wins decoding.
func uniqueJSON(d *json.Decoder) error {
	t, e := d.Token()
	if e != nil {
		return e
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	switch delim {
	case '{':
		seen := map[string]bool{}
		for d.More() {
			key, e := d.Token()
			if e != nil {
				return e
			}
			s, ok := key.(string)
			if !ok || seen[s] {
				return fmt.Errorf("duplicate JSON field")
			}
			seen[s] = true
			if e = uniqueJSON(d); e != nil {
				return e
			}
		}
		_, e = d.Token()
		return e
	case '[':
		for d.More() {
			if e = uniqueJSON(d); e != nil {
				return e
			}
		}
		_, e = d.Token()
		return e
	default:
		return fmt.Errorf("invalid JSON")
	}
}
func decodeCreate(body []byte) (*collectionv1.CreateCrawlRequest, error) {
	bad := func() (*collectionv1.CreateCrawlRequest, error) {
		return nil, status.Error(codes.InvalidArgument, "expected name, url and optional bounded HTTP collection options; duplicate, unknown or invalid fields are not accepted")
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
	for k := range raw {
		if k != "name" && k != "url" && k != "options" {
			return bad()
		}
	}
	r := &collectionv1.CreateCrawlRequest{Options: &collectionv1.CrawlOptions{}}
	if json.Unmarshal(raw["name"], &r.Name) != nil || json.Unmarshal(raw["url"], &r.Url) != nil {
		return bad()
	}
	if b, ok := raw["options"]; ok {
		var o map[string]json.RawMessage
		if json.Unmarshal(b, &o) != nil || o == nil {
			return bad()
		}
		for k, v := range o {
			if string(v) == "null" {
				return bad()
			}
			switch k {
			case "max_bytes":
				if json.Unmarshal(v, &r.Options.MaxBytes) != nil {
					return bad()
				}
			case "timeout_seconds":
				if json.Unmarshal(v, &r.Options.TimeoutSeconds) != nil {
					return bad()
				}
			case "render_javascript":
				if json.Unmarshal(v, &r.Options.RenderJavascript) != nil {
					return bad()
				}
			case "render_wait_ms":
				if json.Unmarshal(v, &r.Options.RenderWaitMs) != nil {
					return bad()
				}
			case "follow_links":
				if json.Unmarshal(v, &r.Options.FollowLinks) != nil {
					return bad()
				}
			case "include_attachments":
				if json.Unmarshal(v, &r.Options.IncludeAttachments) != nil {
					return bad()
				}
			case "max_depth":
				if json.Unmarshal(v, &r.Options.MaxDepth) != nil {
					return bad()
				}
			case "max_documents":
				if json.Unmarshal(v, &r.Options.MaxDocuments) != nil {
					return bad()
				}
			case "max_total_bytes":
				if json.Unmarshal(v, &r.Options.MaxTotalBytes) != nil {
					return bad()
				}
			default:
				return bad()
			}
		}
	}
	return r, nil
}
func (s *Server) create(w http.ResponseWriter, r *http.Request) {
	r.Body = http.MaxBytesReader(w, r.Body, 16<<10)
	body, e := io.ReadAll(r.Body)
	if e != nil {
		failure(w, status.Error(codes.InvalidArgument, "request body exceeds 16 KiB"))
		return
	}
	req, e := decodeCreate(body)
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
	result, e := s.Collection.CreateCrawl(ctx, req)
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 202, map[string]any{"crawl_run_id": result.CrawlRunId, "operation_id": result.OperationId, "status": "accepted"})
}
func (s *Server) getCrawl(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := requestContext(r)
	defer cancel()
	run, e := s.Collection.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	out := crawlView(run)
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
func page(r *http.Request, allowed ...string) (int32, error) {
	permitted := map[string]bool{"limit": true, "cursor": true}
	for _, s := range allowed {
		permitted[s] = true
	}
	for k, v := range r.URL.Query() {
		if !permitted[k] || len(v) != 1 {
			return 0, status.Error(codes.InvalidArgument, "unknown or repeated query parameter")
		}
	}
	raw := r.URL.Query().Get("limit")
	if raw == "" {
		return 20, nil
	}
	v, e := strconv.ParseInt(raw, 10, 32)
	if e != nil || v < 1 || v > 100 {
		return 0, status.Error(codes.InvalidArgument, "limit must be 1..100")
	}
	return int32(v), nil
}
func (s *Server) listCrawls(w http.ResponseWriter, r *http.Request) {
	limit, e := page(r, "q")
	if e != nil {
		failure(w, e)
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	result, e := s.Collection.ListCrawls(ctx, &collectionv1.ListCrawlsRequest{Limit: limit, Cursor: r.URL.Query().Get("cursor"), Q: r.URL.Query().Get("q")})
	if e != nil {
		failure(w, e)
		return
	}
	items := make([]map[string]any, 0, len(result.Items))
	for _, item := range result.Items {
		items = append(items, crawlView(item))
	}
	write(w, 200, map[string]any{"items": items, "next_cursor": result.NextCursor})
}
func (s *Server) listDocuments(w http.ResponseWriter, r *http.Request) {
	if flag := r.URL.Query().Get("include_deleted"); flag != "" && flag != "true" && flag != "false" {
		failure(w, status.Error(codes.InvalidArgument, "include_deleted must be true or false"))
		return
	}
	limit, e := page(r, "q", "include_deleted", "group_id")
	if e != nil {
		failure(w, e)
		return
	}
	ctx, cancel := requestContext(r)
	defer cancel()
	result, e := s.Collection.ListDocuments(ctx, &collectionv1.ListDocumentsRequest{Limit: limit, Cursor: r.URL.Query().Get("cursor"), Q: r.URL.Query().Get("q"), GroupId: r.URL.Query().Get("group_id"), IncludeDeleted: r.URL.Query().Get("include_deleted") == "true"})
	if e != nil {
		failure(w, e)
		return
	}
	items := make([]map[string]any, 0, len(result.Items))
	for _, item := range result.Items {
		items = append(items, documentView(item))
	}
	write(w, 200, map[string]any{"items": items, "next_cursor": result.NextCursor})
}
func (s *Server) getDocument(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := requestContext(r)
	defer cancel()
	doc, e := s.Collection.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, documentView(doc))
}
func (s *Server) getOperation(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := requestContext(r)
	defer cancel()
	op, e := s.Operations.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: r.PathValue("id")})
	if e != nil {
		failure(w, e)
		return
	}
	write(w, 200, operationView(op))
}
func (s *Server) listOperations(w http.ResponseWriter, r *http.Request) {
	limit, e := page(r, "status", "resource_type", "resource_id", "kind", "phase")
	if e != nil {
		failure(w, e)
		return
	}
	q := r.URL.Query()
	ctx, cancel := requestContext(r)
	defer cancel()
	result, e := s.Operations.ListOperations(ctx, &operationv1.ListOperationsRequest{Limit: limit, Cursor: q.Get("cursor"), Status: q.Get("status"), ResourceType: q.Get("resource_type"), ResourceId: q.Get("resource_id"), Kind: q.Get("kind"), Phase: q.Get("phase")})
	if e != nil {
		failure(w, e)
		return
	}
	items := make([]map[string]any, 0, len(result.Operations))
	for _, item := range result.Operations {
		items = append(items, operationView(item))
	}
	write(w, 200, map[string]any{"items": items, "next_cursor": result.NextCursor})
}
func (s *Server) download(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	select {
	case s.downloads <- struct{}{}:
		defer func() { <-s.downloads }()
	case <-ctx.Done():
		failure(w, status.Error(codes.DeadlineExceeded, "download wait expired"))
		return
	}
	req := &collectionv1.GetDocumentRequest{DocumentId: r.PathValue("id"), HttpResponse: strings.HasSuffix(r.URL.Path, "/http-response")}
	doc, e := s.Collection.GetDocument(ctx, req)
	if e != nil {
		failure(w, e)
		return
	}
	if req.HttpResponse {
		if doc.BrowserCapture == nil {
			failure(w, status.Error(codes.NotFound, "HTTP response evidence is unavailable"))
			return
		}
		doc.SizeBytes, doc.Sha256 = doc.BrowserCapture.ResponseSizeBytes, doc.BrowserCapture.ResponseSha256
	}
	if doc.SizeBytes < 0 || doc.SizeBytes > 32<<20 {
		failure(w, status.Error(codes.DataLoss, "invalid source size"))
		return
	}
	stream, e := s.Collection.ReadDocument(ctx, req)
	if e != nil {
		failure(w, e)
		return
	}
	var data bytes.Buffer
	for {
		part, e := stream.Recv()
		if e == io.EOF {
			break
		}
		if e != nil {
			failure(w, e)
			return
		}
		if int64(data.Len()+len(part.Data)) > doc.SizeBytes {
			failure(w, status.Error(codes.DataLoss, "source exceeded recorded size"))
			return
		}
		data.Write(part.Data)
	}
	sum := sha256.Sum256(data.Bytes())
	if int64(data.Len()) != doc.SizeBytes || hex.EncodeToString(sum[:]) != doc.Sha256 {
		failure(w, status.Error(codes.DataLoss, "source integrity check failed"))
		return
	}
	w.Header().Set("Content-Type", "application/octet-stream")
	filename := doc.DocumentId + ".bin"
	if doc.SourceType == "upload" && doc.OriginalFilename != "" {
		filename = doc.OriginalFilename
	}
	w.Header().Set("Content-Disposition", mime.FormatMediaType("attachment", map[string]string{"filename": filename}))
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("Content-Length", strconv.FormatInt(doc.SizeBytes, 10))
	w.Header().Set("X-Content-SHA256", doc.Sha256)
	w.WriteHeader(200)
	_, _ = w.Write(data.Bytes())
}

func sourceIDs(c *collectionv1.CrawlRun) []string {
	if len(c.GetOutputDocumentIds()) > 0 {
		return c.OutputDocumentIds
	}
	if c.GetOutputDocumentId() != "" {
		return []string{c.OutputDocumentId}
	}
	return []string{}
}
