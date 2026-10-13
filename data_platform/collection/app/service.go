package app

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"mime"
	"strings"
	"time"
	"unicode/utf8"

	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/crawl"
	"data-platform/internal/idempotency"
	"data-platform/internal/submission"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const Schema = `
CREATE TABLE IF NOT EXISTS crawl_runs (
 crawl_run_id UUID PRIMARY KEY, operation_id UUID NOT NULL UNIQUE,
 name TEXT NOT NULL, source_url TEXT NOT NULL, options JSONB NOT NULL,
 output_document_id UUID UNIQUE, completion_hash TEXT,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS source_documents (
 document_id UUID PRIMARY KEY, crawl_run_id UUID NOT NULL UNIQUE REFERENCES crawl_runs(crawl_run_id),
 name TEXT NOT NULL, source_url TEXT NOT NULL, final_url TEXT NOT NULL,
 sha256 VARCHAR(64) NOT NULL, size_bytes BIGINT NOT NULL CHECK(size_bytes>=0 AND size_bytes<=33554432),
 content_type TEXT NOT NULL, object_key TEXT NOT NULL UNIQUE, processor_version TEXT NOT NULL,
 processor_image_id TEXT NOT NULL,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS crawl_runs_browse_idx ON crawl_runs(created_at DESC,crawl_run_id DESC);
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS purge_state TEXT NOT NULL DEFAULT '';
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS purged_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS source_purge_fences(document_id UUID PRIMARY KEY);
CREATE INDEX IF NOT EXISTS source_documents_browse_idx ON source_documents(created_at DESC,document_id DESC);
`

type Service struct {
	refinement refinementv1.RefinementServiceClient
	collectionv1.UnimplementedCollectionServiceServer
	pool    *pgxpool.Pool
	ops     operationv1.OperationServiceClient
	store   blob.Store
	reads   chan struct{}
	uploads chan struct{}
}

func New(p *pgxpool.Pool, ops operationv1.OperationServiceClient, store blob.Store) *Service {
	return &Service{pool: p, ops: ops, store: store, reads: make(chan struct{}, 2), uploads: make(chan struct{}, 2)}
}
func InitSchema(ctx context.Context, p *pgxpool.Pool) error {
	_, e := p.Exec(ctx, Schema+uploadSchema+expansionSchema+groupSchema+idempotency.Schema+submission.Schema)
	return e
}
func (s *Service) RunSubmissions(ctx context.Context) {
	submission.Worker{Pool: s.pool, Ensure: func(c context.Context, r *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
		return s.ops.EnsureOperation(c, r)
	}}.Run(ctx)
}

func NormalizeRequest(r *collectionv1.CreateCrawlRequest) (*collectionv1.CreateCrawlRequest, error) {
	name := strings.TrimSpace(r.GetName())
	if name == "" || !utf8.ValidString(name) || utf8.RuneCountInString(name) > 200 || strings.ContainsAny(name, "\x00\r\n") {
		return nil, status.Error(codes.InvalidArgument, "name must contain 1..200 characters")
	}
	raw, e := crawl.NormalizeURL(r.GetUrl())
	if e != nil {
		return nil, status.Error(codes.InvalidArgument, e.Error())
	}
	options, e := crawl.NormalizeOptions(crawl.OptionsFromProto(r.GetOptions()))
	if e != nil {
		return nil, status.Error(codes.InvalidArgument, e.Error())
	}
	if r.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key is required")
	}
	if e = idempotency.ValidateKey(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	return &collectionv1.CreateCrawlRequest{Name: name, Url: raw, Options: options.Proto(), IdempotencyKey: r.GetIdempotencyKey()}, nil
}
func (s *Service) CreateCrawl(ctx context.Context, r *collectionv1.CreateCrawlRequest) (*collectionv1.CrawlAccepted, error) {
	normalized, e := NormalizeRequest(r)
	if e != nil {
		return nil, e
	}
	options := crawl.OptionsFromProto(normalized.Options)
	fingerprint, e := idempotency.Fingerprint(struct {
		Version, Name, URL string
		Options            crawl.Options
	}{"crawl-request-v1", normalized.Name, normalized.Url, options}, nil)
	if e != nil {
		return nil, e
	}
	out := &collectionv1.CrawlAccepted{CrawlRunId: uuid.NewString(), OperationId: uuid.NewString()}
	e = idempotency.Execute(ctx, s.pool, "collection.crawl", normalized.IdempotencyKey, fingerprint, out, func(ctx context.Context, tx pgx.Tx) error {
		encoded, _ := json.Marshal(options)
		if _, e := tx.Exec(ctx, `INSERT INTO crawl_runs(crawl_run_id,operation_id,name,source_url,options) VALUES($1,$2,$3,$4,$5::jsonb)`, out.CrawlRunId, out.OperationId, normalized.Name, normalized.Url, encoded); e != nil {
			return e
		}
		return submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: out.OperationId, Kind: options.ExecutionType(), ExecutionType: options.ExecutionType(), ResourceType: "crawl_run", ResourceId: out.CrawlRunId, MaxAttempts: 1})
	})
	return out, e
}

const runSelect = `SELECT crawl_run_id::text,operation_id::text,name,source_url,options,COALESCE(output_document_id::text,''),created_at,output_document_ids,document_limit_reached,depth_limit_reached,link_limit_reached FROM crawl_runs`

func scanRun(row pgx.Row) (*collectionv1.CrawlRun, error) {
	r := &collectionv1.CrawlRun{}
	var raw, ids []byte
	var t time.Time
	e := row.Scan(&r.CrawlRunId, &r.OperationId, &r.Name, &r.Url, &raw, &r.OutputDocumentId, &t, &ids, &r.DocumentLimitReached, &r.DepthLimitReached, &r.LinkLimitReached)
	if e != nil {
		return nil, e
	}
	var o crawl.Options
	if e = json.Unmarshal(raw, &o); e != nil {
		return nil, e
	}
	if e = json.Unmarshal(ids, &r.OutputDocumentIds); e != nil {
		return nil, e
	}
	if len(r.OutputDocumentIds) == 0 && r.OutputDocumentId != "" {
		r.OutputDocumentIds = []string{r.OutputDocumentId}
	}
	r.Options = o.Proto()
	r.CreatedAt = timestamppb.New(t)
	return r, nil
}
func validID(v string) error {
	id, e := uuid.Parse(v)
	if e != nil || id == uuid.Nil || id.String() != v {
		return status.Error(codes.InvalidArgument, "canonical nonzero UUID required")
	}
	return nil
}
func readError(e error) error {
	if errors.Is(e, pgx.ErrNoRows) {
		return status.Error(codes.NotFound, "resource not found")
	}
	return idempotency.DBError(e)
}
func (s *Service) GetCrawl(ctx context.Context, r *collectionv1.GetCrawlRequest) (*collectionv1.CrawlRun, error) {
	if e := validID(r.GetCrawlRunId()); e != nil {
		return nil, e
	}
	run, e := scanRun(s.pool.QueryRow(ctx, runSelect+` WHERE crawl_run_id=$1`, r.CrawlRunId))
	if e != nil {
		return nil, readError(e)
	}
	info, e := submission.Lookup(ctx, s.pool, "crawl_run", run.CrawlRunId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	run.SubmissionStatus = info.Status
	run.SubmissionErrorCode = info.ErrorCode
	return run, nil
}

func authorized(op *operationv1.Operation, r *collectionv1.CompleteCrawlRequest) bool {
	return op != nil && op.GetOperationId() == r.GetOperationId() && op.GetResourceId() == r.GetCrawlRunId() && op.GetResourceType() == "crawl_run" && (op.GetExecutionType() == "collection.crawl" || op.GetExecutionType() == "collection.browser") && op.GetDispatchProtocol() == 1 && op.GetExecutionStartedAt() != nil && op.GetDispatchToken() == r.GetDispatchToken() && op.GetExecutionInstanceId() == r.GetExecutionInstanceId() && (op.GetStatus() == "running" || op.GetStatus() == "succeeded")
}
func (s *Service) CompleteCrawl(ctx context.Context, r *collectionv1.CompleteCrawlRequest) (*collectionv1.CrawlRun, error) {
	for _, v := range []string{r.GetCrawlRunId(), r.GetOperationId(), r.GetDispatchToken(), r.GetExecutionInstanceId()} {
		if e := validID(v); e != nil {
			return nil, e
		}
	}
	final, e := crawl.NormalizeURL(r.GetFinalUrl())
	if e != nil || final != r.GetFinalUrl() {
		return nil, status.Error(codes.InvalidArgument, "invalid final URL")
	}
	kind, params, e := mime.ParseMediaType(r.GetContentType())
	if e != nil || mime.FormatMediaType(kind, params) != r.GetContentType() || len(r.GetContentType()) > 512 {
		return nil, status.Error(codes.InvalidArgument, "invalid content_type")
	}
	if !blob.ValidDigest(r.GetSha256()) || r.GetSizeBytes() < 0 || r.GetSizeBytes() > 32<<20 || r.GetHttpStatus() < 200 || r.GetHttpStatus() >= 300 || r.GetHttpStatus() == 206 || !strings.HasPrefix(r.GetProcessorImageId(), "sha256:") || !blob.ValidDigest(strings.TrimPrefix(r.GetProcessorImageId(), "sha256:")) {
		return nil, status.Error(codes.InvalidArgument, "invalid source result")
	}
	check := func() error {
		c, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		op, e := s.ops.GetOperation(c, &operationv1.GetOperationRequest{OperationId: r.OperationId})
		if e != nil {
			return e
		}
		if !authorized(op, r) || op.GetExecutionType() != "collection.crawl" {
			return status.Error(codes.FailedPrecondition, "result is not from the authorized crawl process")
		}
		return nil
	}
	if e = check(); e != nil {
		return nil, e
	}
	kind = r.GetContentType()
	fingerprint, _ := idempotency.Fingerprint(struct {
		SHA, URL, Type, Image string
		Size                  int64
		Status                int32
	}{r.Sha256, final, kind, r.ProcessorImageId, r.SizeBytes, r.HttpStatus}, nil)
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	run, e := scanRun(tx.QueryRow(ctx, runSelect+` WHERE crawl_run_id=$1 FOR UPDATE`, r.CrawlRunId))
	if e != nil {
		return nil, readError(e)
	}
	if crawl.OptionsFromProto(run.Options).Expanded() {
		return nil, status.Error(codes.FailedPrecondition, "expanded crawl requires atomic batch completion")
	}
	if run.OperationId != r.OperationId || r.SizeBytes > run.Options.MaxBytes {
		return nil, status.Error(codes.FailedPrecondition, "result does not match requested crawl")
	}
	if run.OutputDocumentId != "" {
		var previous string
		if e = tx.QueryRow(ctx, `SELECT completion_hash FROM crawl_runs WHERE crawl_run_id=$1`, run.CrawlRunId).Scan(&previous); e != nil {
			return nil, idempotency.DBError(e)
		}
		if previous != fingerprint {
			return nil, status.Error(codes.AlreadyExists, "immutable crawl result differs")
		}
		return run, nil
	}
	key, _ := blob.Key(run.CrawlRunId, r.Sha256)
	if e = blob.Verify(ctx, s.store, key, r.Sha256, r.SizeBytes); e != nil {
		return nil, status.Error(codes.FailedPrecondition, "stored source integrity verification failed")
	}
	if e = check(); e != nil {
		return nil, e
	}
	docID := uuid.NewString()
	_, e = tx.Exec(ctx, `INSERT INTO source_documents(document_id,crawl_run_id,name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`, docID, run.CrawlRunId, run.Name, run.Url, final, r.Sha256, r.SizeBytes, kind, key, crawl.ProcessorVersion, r.ProcessorImageId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	_, e = tx.Exec(ctx, `UPDATE crawl_runs SET output_document_id=$2,completion_hash=$3 WHERE crawl_run_id=$1`, run.CrawlRunId, docID, fingerprint)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	run.OutputDocumentId = docID
	run.OutputDocumentIds = []string{docID}
	return run, nil
}

const docSelect = `SELECT document_id::text,COALESCE(crawl_run_id::text,''),name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id,created_at,source_type,original_filename,format,format_version,parent_url,crawl_depth,crawl_role,browser_capture,deleted_at,purge_state,purged_at FROM source_documents`

func (s *Service) scanDoc(row pgx.Row) (*collectionv1.SourceDocument, error) {
	d := &collectionv1.SourceDocument{}
	var key string
	var capture []byte
	var t time.Time
	var deleted, purged *time.Time
	e := row.Scan(&d.DocumentId, &d.CrawlRunId, &d.Name, &d.SourceUrl, &d.FinalUrl, &d.Sha256, &d.SizeBytes, &d.ContentType, &key, &d.ProcessorVersion, &d.ProcessorImageId, &t, &d.SourceType, &d.OriginalFilename, &d.Format, &d.FormatVersion, &d.ParentUrl, &d.CrawlDepth, &d.CrawlRole, &capture, &deleted, &d.PurgeState, &purged)
	if e != nil {
		return nil, e
	}
	if len(capture) > 0 && string(capture) != "null" {
		d.BrowserCapture = &collectionv1.BrowserCapture{}
		if e = json.Unmarshal(capture, d.BrowserCapture); e != nil {
			return nil, e
		}
		responseKey, e := blob.BrowserResponseKey(d.CrawlRunId, d.SourceUrl, d.BrowserCapture.ResponseSha256)
		if e != nil {
			return nil, e
		}
		d.BrowserCapture.ResponseArtifactUri = s.store.URI(responseKey)
	}
	d.ArtifactUri = s.store.URI(key)
	d.State = "ready"
	if d.PurgeState != "" {
		d.State = d.PurgeState
		d.ArtifactUri = ""
		if d.BrowserCapture != nil {
			d.BrowserCapture.ResponseArtifactUri = ""
		}
	}
	d.CreatedAt = timestamppb.New(t)
	if purged != nil {
		d.PurgedAt = timestamppb.New(*purged)
	}
	if deleted != nil {
		d.DeletedAt = timestamppb.New(*deleted)
	}
	return d, nil
}
func (s *Service) GetDocument(ctx context.Context, r *collectionv1.GetDocumentRequest) (*collectionv1.SourceDocument, error) {
	if e := validID(r.GetDocumentId()); e != nil {
		return nil, e
	}
	d, e := s.scanDoc(s.pool.QueryRow(ctx, docSelect+` WHERE document_id=$1`, r.DocumentId))
	if e != nil {
		return nil, readError(e)
	}
	if e = s.attachDocumentGroups(ctx, []*collectionv1.SourceDocument{d}); e != nil {
		return nil, e
	}
	return d, nil
}
func (s *Service) ReadDocument(r *collectionv1.GetDocumentRequest, stream collectionv1.CollectionService_ReadDocumentServer) error {
	ctx := stream.Context()
	select {
	case s.reads <- struct{}{}:
		defer func() { <-s.reads }()
	case <-ctx.Done():
		return status.FromContextError(ctx.Err()).Err()
	}
	d, e := s.GetDocument(ctx, r)
	if e != nil {
		return e
	}
	if d.PurgeState != "" {
		return status.Error(codes.FailedPrecondition, "source files are permanently deleted or deletion is pending")
	}
	var key string
	if e = s.pool.QueryRow(ctx, `SELECT object_key FROM source_documents WHERE document_id=$1`, d.DocumentId).Scan(&key); e != nil {
		return readError(e)
	}
	if r.HttpResponse {
		if d.BrowserCapture == nil {
			return status.Error(codes.NotFound, "HTTP response evidence is only available for browser captures")
		}
		key, e = blob.BrowserResponseKey(d.CrawlRunId, d.SourceUrl, d.BrowserCapture.ResponseSha256)
		if e != nil {
			return status.Error(codes.DataLoss, "invalid response identity")
		}
		d.SizeBytes, d.Sha256 = d.BrowserCapture.ResponseSizeBytes, d.BrowserCapture.ResponseSha256
	}
	obj, e := s.store.Open(ctx, key)
	if e != nil {
		return status.Error(codes.Unavailable, "source object is unavailable")
	}
	defer obj.Close()
	data, e := io.ReadAll(io.LimitReader(obj, d.SizeBytes+1))
	if e != nil {
		return status.Error(codes.Unavailable, "source read failed")
	}
	hash := sha256.Sum256(data)
	if int64(len(data)) != d.SizeBytes || hex.EncodeToString(hash[:]) != d.Sha256 {
		return status.Error(codes.DataLoss, "source integrity check failed")
	}
	for len(data) > 0 {
		n := 64 << 10
		if n > len(data) {
			n = len(data)
		}
		if e = stream.Send(&collectionv1.DocumentChunk{Data: data[:n]}); e != nil {
			return e
		}
		data = data[n:]
	}
	return nil
}
