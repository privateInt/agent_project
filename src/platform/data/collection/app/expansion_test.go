package app

import (
	"context"
	"encoding/json"
	"io"
	"strings"
	"testing"

	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/crawl"
	"data-platform/internal/idempotency"
	"data-platform/internal/submission"
	"data-platform/internal/testsupport"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func manifest() *collectionv1.CompleteCrawlBatchRequest {
	return &collectionv1.CompleteCrawlBatchRequest{OperationId: "op", ProcessorImageId: "sha256:" + strings.Repeat("a", 64), Sources: []*collectionv1.CrawlSourceResult{
		{SourceUrl: "https://example.com/", FinalUrl: "https://example.com/", Role: "seed", Sha256: strings.Repeat("a", 64), SizeBytes: 8, ContentType: "text/html", HttpStatus: 200},
		{SourceUrl: "https://example.com/x.pdf", FinalUrl: "https://example.com/x.pdf", ParentUrl: "https://example.com/", Depth: 1, Role: "attachment", Sha256: strings.Repeat("a", 64), SizeBytes: 8, ContentType: "application/pdf", HttpStatus: 200},
	}}
}
func TestBatchManifestEnforcesScopeAndProvenance(t *testing.T) {
	options, _ := crawl.NormalizeOptions(crawl.Options{FollowLinks: true, IncludeAttachments: true})
	run := &collectionv1.CrawlRun{Url: "https://example.com/", OperationId: "op", Options: options.Proto()}
	if e := validateBatch(run, manifest()); e != nil {
		t.Fatal(e)
	}
	for _, change := range []func(*collectionv1.CompleteCrawlBatchRequest){
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources = nil },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1] = nil },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].SourceUrl = r.Sources[0].SourceUrl },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].FinalUrl = "https://elsewhere.example/x" },
		func(r *collectionv1.CompleteCrawlBatchRequest) {
			r.Sources[1].ParentUrl = "https://example.com/unknown"
		},
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].Depth = 3 },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].Role = "page" },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].HttpStatus = 206 },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[1].SizeBytes = 33 << 20 },
		func(r *collectionv1.CompleteCrawlBatchRequest) { r.Sources[0].SourceUrl = "https://example.com/other" },
	} {
		r := manifest()
		change(r)
		if e := validateBatch(run, r); e == nil {
			t.Fatal("invalid manifest accepted", r)
		}
	}
	run.Options.IncludeAttachments = false
	if e := validateBatch(run, manifest()); e == nil {
		t.Fatal("unrequested attachments")
	}
}
func prepareBatch(t *testing.T) (*Service, *memoryStore, *collectionv1.CompleteCrawlBatchRequest) {
	t.Helper()
	s, ops, m := fixture(t)
	input := request()
	input.Options = &collectionv1.CrawlOptions{FollowLinks: true, IncludeAttachments: true}
	old := prepareResultFor(t, s, ops, m, input)
	r := manifest()
	r.CrawlRunId = old.CrawlRunId
	r.OperationId = old.OperationId
	r.DispatchToken = old.DispatchToken
	r.ExecutionInstanceId = old.ExecutionInstanceId
	for _, item := range r.Sources {
		item.Sha256 = old.Sha256
		item.SizeBytes = old.SizeBytes
	}
	if _, e := s.CompleteCrawl(context.Background(), old); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("expanded request accepted legacy completion", e)
	}
	return s, m, r
}

type selectiveStore struct {
	*memoryStore
	failLast bool
}

func (m *selectiveStore) Open(c context.Context, key string) (io.ReadCloser, error) {
	m.opens++
	if m.failLast && m.opens == 2 {
		return io.NopCloser(strings.NewReader("tampered")), nil
	}
	return io.NopCloser(strings.NewReader(m.body)), nil
}
func TestBatchIntegrityAndLostCommitReplyAreAtomic(t *testing.T) {
	s, m, r := prepareBatch(t)
	ctx := context.Background()
	store := &selectiveStore{memoryStore: m, failLast: true}
	s.store = store
	if _, e := s.CompleteCrawlBatch(ctx, r); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("partial corrupt batch accepted", e)
	}
	var n int
	s.pool.QueryRow(ctx, `SELECT count(*) FROM source_documents`).Scan(&n)
	if n != 0 {
		t.Fatal("partial batch registered")
	}
	store.failLast = false
	first, e := s.CompleteCrawlBatch(ctx, r)
	if e != nil || len(first.GetOutputDocumentIds()) != 2 {
		t.Fatal(first, e)
	}
	replay, e := s.CompleteCrawlBatch(ctx, proto.Clone(r).(*collectionv1.CompleteCrawlBatchRequest))
	if e != nil || !equalIDs(first.OutputDocumentIds, replay.OutputDocumentIds) {
		t.Fatal("replayed IDs differ", e)
	}
	run, e := s.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: r.CrawlRunId})
	if e != nil || !equalIDs(first.OutputDocumentIds, run.OutputDocumentIds) {
		t.Fatal("GET manifest", run, e)
	}
	for i, id := range run.OutputDocumentIds {
		d, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: id})
		if e != nil || d.SourceUrl != r.Sources[i].SourceUrl || d.ParentUrl != r.Sources[i].ParentUrl || d.CrawlRole != r.Sources[i].Role || d.ProcessorVersion != crawl.ScopeProcessorVersion {
			t.Fatal("provenance", d, e)
		}
	}
	changed := proto.Clone(r).(*collectionv1.CompleteCrawlBatchRequest)
	changed.DocumentLimitReached = true
	if _, e := s.CompleteCrawlBatch(ctx, changed); status.Code(e) != codes.AlreadyExists {
		t.Fatal("result overwritten", e)
	}
	// Applying startup migration again must preserve both old and expanded rows.
	if e := InitSchema(ctx, s.pool); e != nil {
		t.Fatal("repeat migration", e)
	}
}
func equalIDs(a, b []string) bool { return strings.Join(a, ",") == strings.Join(b, ",") }
func TestBatchDatabaseFailureRollsBackEveryDocument(t *testing.T) {
	s, _, r := prepareBatch(t)
	ctx := context.Background()
	if _, e := s.pool.Exec(ctx, `ALTER TABLE source_documents ADD CONSTRAINT fail_second CHECK(crawl_role<>'attachment')`); e != nil {
		t.Fatal(e)
	}
	if _, e := s.CompleteCrawlBatch(ctx, r); e == nil {
		t.Fatal("injected INSERT failure ignored")
	}
	var n int
	s.pool.QueryRow(ctx, `SELECT count(*) FROM source_documents`).Scan(&n)
	if n != 0 {
		t.Fatal("partial INSERT retained")
	}
	run, e := s.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: r.CrawlRunId})
	if e != nil || run.OutputDocumentId != "" || len(run.OutputDocumentIds) != 0 {
		t.Fatal("premature completion", run, e)
	}
}
func TestExpansionRequestReplayAndLegacyFingerprint(t *testing.T) {
	s, _, _ := fixture(t)
	ctx := context.Background()
	r := request()
	// Pre-expansion fingerprint exactly matches the old normalized request.
	legacy, _ := idempotency.Fingerprint(struct {
		Version, Name, URL string
		Options            struct {
			MaxBytes       int64 `json:"max_bytes"`
			TimeoutSeconds int32 `json:"timeout_seconds"`
		}
	}{Version: "crawl-request-v1", Name: r.Name, URL: r.Url, Options: struct {
		MaxBytes       int64 `json:"max_bytes"`
		TimeoutSeconds int32 `json:"timeout_seconds"`
	}{8 << 20, 120}}, nil)
	normalized, _ := NormalizeRequest(r)
	options := crawl.OptionsFromProto(normalized.Options)
	current, _ := idempotency.Fingerprint(struct {
		Version, Name, URL string
		Options            crawl.Options
	}{"crawl-request-v1", normalized.Name, normalized.Url, options}, nil)
	if legacy != current {
		t.Fatal("legacy key changed")
	}
	r.Options = &collectionv1.CrawlOptions{FollowLinks: true, IncludeAttachments: true}
	a, e := s.CreateCrawl(ctx, r)
	if e != nil {
		t.Fatal(e)
	}
	replay, e := s.CreateCrawl(ctx, r)
	if e != nil || a.CrawlRunId != replay.CrawlRunId {
		t.Fatal("request replay", e)
	}
	changed := proto.Clone(r).(*collectionv1.CreateCrawlRequest)
	changed.Options.IncludeAttachments = false
	if _, e := s.CreateCrawl(ctx, changed); status.Code(e) != codes.AlreadyExists {
		t.Fatal("changed scope reused key", e)
	}
	run, e := s.GetCrawl(ctx, &collectionv1.GetCrawlRequest{CrawlRunId: a.CrawlRunId})
	if e != nil || run.Options.MaxDocuments != 10 || !run.Options.IncludeAttachments {
		t.Fatal(run, e)
	}
	var raw []byte
	if e := s.pool.QueryRow(ctx, `SELECT options FROM crawl_runs WHERE crawl_run_id=$1`, a.CrawlRunId).Scan(&raw); e != nil {
		t.Fatal(e)
	}
	var stored crawl.Options
	if json.Unmarshal(raw, &stored) != nil || !stored.IncludeAttachments {
		t.Fatal("scope lost")
	}
}

func TestExpansionMigratesLegacySourceWithoutChangingIDs(t *testing.T) {
	ctx := context.Background()
	pool := testsupport.Postgres(t)
	if _, e := pool.Exec(ctx, Schema+uploadSchema+idempotency.Schema+submission.Schema); e != nil {
		t.Fatal(e)
	}
	runID := "11111111-1111-4111-8111-111111111111"
	opID := "22222222-2222-4222-8222-222222222222"
	docID := "33333333-3333-4333-8333-333333333333"
	if _, e := pool.Exec(ctx, `INSERT INTO crawl_runs(crawl_run_id,operation_id,name,source_url,options,output_document_id) VALUES($1,$2,'legacy','https://example.com/',$3::jsonb,$4)`, runID, opID, `{"max_bytes":8388608,"timeout_seconds":120}`, docID); e != nil {
		t.Fatal(e)
	}
	if _, e := pool.Exec(ctx, `INSERT INTO source_documents(document_id,crawl_run_id,name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id) VALUES($1,$2,'legacy','https://example.com/','https://example.com/',$3,8,'text/html','original-key','http-source-v1',$4)`, docID, runID, strings.Repeat("a", 64), "sha256:"+strings.Repeat("b", 64)); e != nil {
		t.Fatal(e)
	}
	for i := 0; i < 2; i++ {
		if e := InitSchema(ctx, pool); e != nil {
			t.Fatal(e)
		}
	}
	run, e := scanRun(pool.QueryRow(ctx, runSelect+` WHERE crawl_run_id=$1`, runID))
	if e != nil || run.OutputDocumentId != docID || len(run.OutputDocumentIds) != 1 || run.OutputDocumentIds[0] != docID {
		t.Fatal(run, e)
	}
	s := New(pool, nil, &memoryStore{})
	doc, e := s.GetDocument(ctx, &collectionv1.GetDocumentRequest{DocumentId: docID})
	if e != nil || doc.ArtifactUri != "s3://data-sources/original-key" || doc.ProcessorVersion != "http-source-v1" {
		t.Fatal(doc, e)
	}
}
