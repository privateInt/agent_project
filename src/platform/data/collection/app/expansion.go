package app

import (
	"context"
	"encoding/json"
	"mime"
	"strings"
	"time"

	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/blob"
	"data-platform/internal/crawl"
	"data-platform/internal/idempotency"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Startup migration is additive apart from replacing the former one-document
// constraint with uniqueness by (crawl, requested URL). Old IDs/keys stay intact.
const expansionSchema = `
ALTER TABLE source_documents DROP CONSTRAINT IF EXISTS source_documents_crawl_run_id_key;
CREATE UNIQUE INDEX IF NOT EXISTS source_documents_crawl_url_idx ON source_documents(crawl_run_id,md5(source_url)) WHERE crawl_run_id IS NOT NULL;
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS browser_capture JSONB;
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS parent_url TEXT NOT NULL DEFAULT '';
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS crawl_depth INTEGER NOT NULL DEFAULT 0;
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS crawl_role TEXT NOT NULL DEFAULT '';
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS output_document_ids JSONB NOT NULL DEFAULT '[]';
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS document_limit_reached BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS depth_limit_reached BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS link_limit_reached BOOLEAN NOT NULL DEFAULT FALSE;
`

func validateBatch(run *collectionv1.CrawlRun, r *collectionv1.CompleteCrawlBatchRequest) error {
	bad := func() error {
		return status.Error(codes.InvalidArgument, "batch source manifest does not match requested scope")
	}
	o, e := crawl.NormalizeOptions(crawl.OptionsFromProto(run.Options))
	if e != nil || !o.Expanded() || run.OperationId != r.OperationId {
		return bad()
	}
	if len(r.Sources) < 1 || len(r.Sources) > int(o.MaxDocuments) {
		return bad()
	}
	seen := map[string]bool{}
	parents := map[string]map[int32]bool{}
	var total, networkBytes int64
	var networkRequests int32
	for i, item := range r.Sources {
		if item == nil {
			return bad()
		}
		for _, raw := range []string{item.SourceUrl, item.FinalUrl} {
			u, e := crawl.NormalizeURL(raw)
			if e != nil || u != raw || !crawl.InScope(run.Url, u) {
				return bad()
			}
		}
		if seen[item.SourceUrl] {
			return bad()
		}
		seen[item.SourceUrl] = true
		kind, params, e := mime.ParseMediaType(item.ContentType)
		if e != nil || mime.FormatMediaType(kind, params) != item.ContentType || len(item.ContentType) > 512 {
			return bad()
		}
		if !blob.ValidDigest(item.Sha256) || item.SizeBytes < 0 || item.SizeBytes > o.MaxBytes || item.HttpStatus < 200 || item.HttpStatus >= 300 || item.HttpStatus == 206 {
			return bad()
		}
		if crawl.ValidateCapture(o, item.Role, item.ContentType, item.BrowserCapture) != nil {
			return bad()
		}
		if item.BrowserCapture != nil {
			if item.BrowserCapture.ResponseArtifactUri != "" {
				return bad()
			}
			networkBytes += item.BrowserCapture.NetworkBytes
			networkRequests += item.BrowserCapture.NetworkRequests
			if networkBytes > crawl.BrowserRunBytes || networkRequests > crawl.BrowserRunRequests {
				return bad()
			}
			total += item.BrowserCapture.ResponseSizeBytes
		}
		total += item.SizeBytes
		if total > o.MaxTotalBytes {
			return bad()
		}
		if i == 0 {
			if item.SourceUrl != run.Url || item.ParentUrl != "" || item.Depth != 0 || item.Role != "seed" {
				return bad()
			}
		} else {
			depths := parents[item.ParentUrl]
			if !depths[item.Depth-1] || !crawl.InScope(item.ParentUrl, item.SourceUrl) {
				return bad()
			}
			switch item.Role {
			case "page":
				if !o.FollowLinks || item.Depth > o.MaxDepth || !crawl.IsHTML(item.ContentType) {
					return bad()
				}
			case "attachment":
				if !o.IncludeAttachments || item.Depth > o.MaxDepth+1 {
					return bad()
				}
			default:
				return bad()
			}
		}
		if item.Role != "attachment" && crawl.IsHTML(item.ContentType) {
			if parents[item.FinalUrl] == nil {
				parents[item.FinalUrl] = map[int32]bool{}
			}
			parents[item.FinalUrl][item.Depth] = true
		}
	}
	return nil
}

func (s *Service) CompleteCrawlBatch(ctx context.Context, r *collectionv1.CompleteCrawlBatchRequest) (*collectionv1.CrawlRun, error) {
	for _, v := range []string{r.GetCrawlRunId(), r.GetOperationId(), r.GetDispatchToken(), r.GetExecutionInstanceId()} {
		if e := validID(v); e != nil {
			return nil, e
		}
	}
	if !strings.HasPrefix(r.GetProcessorImageId(), "sha256:") || !blob.ValidDigest(strings.TrimPrefix(r.GetProcessorImageId(), "sha256:")) {
		return nil, status.Error(codes.InvalidArgument, "invalid processor image identity")
	}
	expectedKind := ""
	grant := &collectionv1.CompleteCrawlRequest{CrawlRunId: r.CrawlRunId, OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId}
	check := func() error {
		c, cancel := context.WithTimeout(ctx, 5*time.Second)
		defer cancel()
		op, e := s.ops.GetOperation(c, &operationv1.GetOperationRequest{OperationId: r.OperationId})
		if e != nil {
			return e
		}
		if !authorized(op, grant) || (expectedKind != "" && op.GetExecutionType() != expectedKind) {
			return status.Error(codes.FailedPrecondition, "result is not from the authorized crawl process")
		}
		return nil
	}
	if e := check(); e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer idempotency.Rollback(tx)
	run, e := scanRun(tx.QueryRow(ctx, runSelect+` WHERE crawl_run_id=$1 FOR UPDATE`, r.CrawlRunId))
	if e != nil {
		return nil, readError(e)
	}
	expectedKind = crawl.OptionsFromProto(run.Options).ExecutionType()
	if e = check(); e != nil {
		return nil, e
	}
	if e = validateBatch(run, r); e != nil {
		return nil, e
	}
	fingerprint, e := idempotency.Fingerprint(struct {
		Version, Image          string
		Sources                 []*collectionv1.CrawlSourceResult
		Documents, Depth, Links bool
	}{crawl.OptionsFromProto(run.Options).BatchProcessor(), r.ProcessorImageId, r.Sources, r.DocumentLimitReached, r.DepthLimitReached, r.LinkLimitReached}, nil)
	if e != nil {
		return nil, e
	}
	if run.OutputDocumentId != "" {
		var previous string
		if e = tx.QueryRow(ctx, `SELECT completion_hash FROM crawl_runs WHERE crawl_run_id=$1`, run.CrawlRunId).Scan(&previous); e != nil {
			return nil, idempotency.DBError(e)
		}
		if previous != fingerprint {
			return nil, status.Error(codes.AlreadyExists, "immutable crawl batch differs")
		}
		return run, nil
	}
	keys := make([]string, len(r.Sources))
	for i, item := range r.Sources {
		keys[i], e = blob.CrawlItemKey(run.CrawlRunId, item.SourceUrl, item.Sha256)
		if e != nil {
			return nil, e
		}
		if item.BrowserCapture != nil {
			c := item.BrowserCapture
			key, _ := blob.BrowserResponseKey(run.CrawlRunId, item.SourceUrl, c.ResponseSha256)
			if e = blob.Verify(ctx, s.store, key, c.ResponseSha256, c.ResponseSizeBytes); e != nil {
				return nil, status.Error(codes.FailedPrecondition, "stored HTTP response integrity verification failed")
			}
		}
		if e = blob.Verify(ctx, s.store, keys[i], item.Sha256, item.SizeBytes); e != nil {
			return nil, status.Error(codes.FailedPrecondition, "stored batch source integrity verification failed")
		}
	}
	if e = check(); e != nil {
		return nil, e
	}
	ids := make([]string, len(r.Sources))
	for i, item := range r.Sources {
		ids[i] = uuid.NewString()
		var capture any
		if item.BrowserCapture != nil {
			encoded, _ := json.Marshal(item.BrowserCapture)
			capture = encoded
		}
		_, e = tx.Exec(ctx, `INSERT INTO source_documents(document_id,crawl_run_id,name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id,parent_url,crawl_depth,crawl_role,browser_capture) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15::jsonb)`, ids[i], run.CrawlRunId, run.Name, item.SourceUrl, item.FinalUrl, item.Sha256, item.SizeBytes, item.ContentType, keys[i], crawl.OptionsFromProto(run.Options).BatchProcessor(), r.ProcessorImageId, item.ParentUrl, item.Depth, item.Role, capture)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
	}
	encoded, _ := json.Marshal(ids)
	_, e = tx.Exec(ctx, `UPDATE crawl_runs SET output_document_id=$2,completion_hash=$3,output_document_ids=$4::jsonb,document_limit_reached=$5,depth_limit_reached=$6,link_limit_reached=$7 WHERE crawl_run_id=$1`, run.CrawlRunId, ids[0], fingerprint, encoded, r.DocumentLimitReached, r.DepthLimitReached, r.LinkLimitReached)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	run.OutputDocumentId = ids[0]
	run.OutputDocumentIds = ids
	run.DocumentLimitReached = r.DocumentLimitReached
	run.DepthLimitReached = r.DepthLimitReached
	run.LinkLimitReached = r.LinkLimitReached
	return run, nil
}
