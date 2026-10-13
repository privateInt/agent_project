package app

import (
	"context"
	"encoding/json"
	"errors"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	chunkingv1 "knowledge-platform/gen/chunking/v1"
	refinementv1 "knowledge-platform/gen/data/refinement/v1"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/blob"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/idempotency"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/listing"
	"knowledge-platform/internal/submission"
	"sort"
	"strings"
	"time"
	"unicode/utf8"
)

// Catalog mutations serialize at the logical index; batch/cleanup at its version.
// No method acquires a parent lock after acquiring a version lock.
type IndexStore interface {
	Ensure(context.Context, indexing.Version) error
	Bulk(context.Context, indexing.Version, []indexing.Record) error
	Seal(context.Context, indexing.Version) error
	Delete(context.Context, indexing.Version) error
}
type IndexService struct {
	indexingv1.UnimplementedIndexingServiceServer
	chunks      *Service
	model       embeddingv1.EmbeddingServiceClient
	reranker    rerankingv1.RerankingServiceClient
	references  refinementv1.RefinementServiceClient
	search      IndexStore
	searchReads chan struct{}
}

func NewIndexes(c *Service, m embeddingv1.EmbeddingServiceClient, r refinementv1.RefinementServiceClient, s IndexStore, reranker rerankingv1.RerankingServiceClient) *IndexService {
	return &IndexService{chunks: c, model: m, reranker: reranker, references: r, search: s, searchReads: make(chan struct{}, 8)}
}

const indexSchema = `
CREATE TABLE IF NOT EXISTS logical_indexes (
 index_id UUID PRIMARY KEY,name TEXT NOT NULL,embedding_deployment_id UUID NOT NULL,
 active_version_id TEXT NOT NULL DEFAULT '',revision BIGINT NOT NULL DEFAULT 1,
 state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','deleting','deleted')),
 created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS index_versions (
 index_version_id UUID PRIMARY KEY,index_id UUID NOT NULL REFERENCES logical_indexes(index_id),
 operation_id UUID NOT NULL UNIQUE,spec_json BYTEA NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('protecting','queued','indexing','ready','failed','deleting','deleted')),
 chunk_count INTEGER NOT NULL DEFAULT 0,indexed_count INTEGER NOT NULL DEFAULT 0,
 reference_state TEXT NOT NULL DEFAULT 'pending',error_code TEXT NOT NULL DEFAULT '',error_message TEXT NOT NULL DEFAULT '',
 physical_started BOOLEAN NOT NULL DEFAULT false,
 created_at TIMESTAMPTZ NOT NULL DEFAULT now(),updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS index_versions_browse_idx ON index_versions(index_id,created_at DESC,index_version_id DESC);
CREATE INDEX IF NOT EXISTS index_versions_work_idx ON index_versions(updated_at) WHERE state IN ('protecting','queued','indexing','deleting');
CREATE TABLE IF NOT EXISTS index_batches (
 index_version_id UUID NOT NULL REFERENCES index_versions(index_version_id),offset_value INTEGER NOT NULL,
 receipt_hash TEXT NOT NULL,chunk_count INTEGER NOT NULL,committed BOOLEAN NOT NULL DEFAULT false,
 PRIMARY KEY(index_version_id,offset_value)
);
`

func jsonIndex(v any) *indexingv1.JSONResponse {
	b, _ := json.Marshal(v)
	return &indexingv1.JSONResponse{Json: b}
}
func indexError(e error) error {
	if errors.Is(e, pgx.ErrNoRows) {
		return missing("index or version not found")
	}
	return idempotency.DBError(e)
}
func (s *IndexService) enabled() error {
	if s.search == nil || s.model == nil || s.references == nil {
		return unavailable("INDEXING_NOT_CONFIGURED: configure OpenSearch before building indexes")
	}
	return nil
}
func keyRequired(key string) error {
	if key == "" {
		return invalid("Idempotency-Key is required")
	}
	return idempotency.ValidateKey(key)
}

const indexSelect = `SELECT index_id::text,name,embedding_deployment_id::text,active_version_id,revision,state,created_at FROM logical_indexes`

func scanIndex(row pgx.Row) (indexing.Index, error) {
	var v indexing.Index
	var t time.Time
	e := row.Scan(&v.ID, &v.Name, &v.DeploymentID, &v.ActiveVersionID, &v.Revision, &v.State, &t)
	v.CreatedAt = t.UTC().Format(time.RFC3339Nano)
	return v, e
}

const versionSelect = `SELECT spec_json,state,chunk_count,indexed_count,reference_state,error_code,error_message,created_at FROM index_versions`

func scanVersion(row pgx.Row) (indexing.Version, error) {
	var v indexing.Version
	var raw []byte
	var state, refs, code, msg string
	var count, next int
	var t time.Time
	e := row.Scan(&raw, &state, &count, &next, &refs, &code, &msg, &t)
	if e != nil {
		return v, e
	}
	if e = json.Unmarshal(raw, &v); e != nil {
		return v, e
	}
	v.State = state
	v.Count = count
	v.Next = next
	v.ReferenceState = refs
	v.ErrorCode = code
	v.ErrorMessage = msg
	v.CreatedAt = t.UTC().Format(time.RFC3339Nano)
	return v, nil
}
func (s *IndexService) CreateIndex(ctx context.Context, r *indexingv1.CreateIndexRequest) (*indexingv1.JSONResponse, error) {
	if e := s.enabled(); e != nil {
		return nil, e
	}
	name := strings.TrimSpace(r.GetName())
	if name == "" || !utf8.ValidString(name) || utf8.RuneCountInString(name) > 120 || strings.ContainsRune(name, 0) {
		return nil, invalid("name must contain 1..120 characters")
	}
	if e := validID(r.GetEmbeddingDeploymentId()); e != nil {
		return nil, e
	}
	if e := keyRequired(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	out := &indexingv1.JSONResponse{}
	fp := chunking.HashJSON([]string{"index-create-v1", name, r.EmbeddingDeploymentId})
	e := idempotency.Execute(ctx, s.chunks.pool, "index.create", r.IdempotencyKey, fp, out, func(ctx context.Context, tx pgx.Tx) error {
		v, e := scanIndex(tx.QueryRow(ctx, `INSERT INTO logical_indexes(index_id,name,embedding_deployment_id) VALUES($1,$2,$3) RETURNING index_id::text,name,embedding_deployment_id::text,active_version_id,revision,state,created_at`, uuid.NewString(), name, r.EmbeddingDeploymentId))
		if e != nil {
			return e
		}
		out.Json = jsonIndex(v).Json
		return nil
	})
	return out, e
}
func (s *IndexService) GetIndex(ctx context.Context, r *indexingv1.IndexRequest) (*indexingv1.JSONResponse, error) {
	if e := validID(r.GetIndexId()); e != nil {
		return nil, e
	}
	v, e := scanIndex(s.chunks.pool.QueryRow(ctx, indexSelect+` WHERE index_id=$1`, r.IndexId))
	if e != nil {
		return nil, indexError(e)
	}
	return jsonIndex(v), nil
}
func (s *IndexService) GetVersion(ctx context.Context, r *indexingv1.VersionRequest) (*indexingv1.JSONResponse, error) {
	if e := validID(r.GetIndexVersionId()); e != nil {
		return nil, e
	}
	v, e := scanVersion(s.chunks.pool.QueryRow(ctx, versionSelect+` WHERE index_version_id=$1`, r.IndexVersionId))
	if e != nil {
		return nil, indexError(e)
	}
	return jsonIndex(v), nil
}
func (s *IndexService) ListIndexes(ctx context.Context, r *indexingv1.ListRequest) (*indexingv1.JSONResponse, error) {
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "indexes")
	if e != nil {
		return nil, e
	}
	q := listing.Query{}
	suffix := q.Finish(page, "created_at", "index_id")
	rows, e := s.chunks.pool.Query(ctx, indexSelect+suffix, q.Args...)
	if e != nil {
		return nil, indexError(e)
	}
	defer rows.Close()
	items := []indexing.Index{}
	next := ""
	for rows.Next() {
		if len(items) == page.Limit {
			last := items[len(items)-1]
			t, _ := time.Parse(time.RFC3339Nano, last.CreatedAt)
			next = page.Next(t, last.ID)
			break
		}
		v, e := scanIndex(rows)
		if e != nil {
			return nil, indexError(e)
		}
		items = append(items, v)
	}
	if e = rows.Err(); e != nil {
		return nil, indexError(e)
	}
	return jsonIndex(map[string]any{"items": items, "next_cursor": next}), nil
}
func (s *IndexService) ListVersions(ctx context.Context, r *indexingv1.ListRequest) (*indexingv1.JSONResponse, error) {
	if e := validID(r.GetIndexId()); e != nil {
		return nil, e
	}
	page, e := listing.New(r.GetLimit(), r.GetCursor(), "index-versions", r.IndexId)
	if e != nil {
		return nil, e
	}
	q := listing.Query{}
	q.Equal("index_id", r.IndexId)
	suffix := q.Finish(page, "created_at", "index_version_id")
	rows, e := s.chunks.pool.Query(ctx, versionSelect+suffix, q.Args...)
	if e != nil {
		return nil, indexError(e)
	}
	defer rows.Close()
	items := []indexing.Version{}
	next := ""
	for rows.Next() {
		if len(items) == page.Limit {
			last := items[len(items)-1]
			t, _ := time.Parse(time.RFC3339Nano, last.CreatedAt)
			next = page.Next(t, last.ID)
			break
		}
		v, e := scanVersion(rows)
		if e != nil {
			return nil, indexError(e)
		}
		items = append(items, v)
	}
	if e = rows.Err(); e != nil {
		return nil, indexError(e)
	}
	return jsonIndex(map[string]any{"items": items, "next_cursor": next}), nil
}

type newChunkRequest struct {
	Name         string   `json:"name"`
	CanonicalIDs []string `json:"canonical_ids"`
	Chunker      struct {
		ID      string          `json:"id"`
		Options json.RawMessage `json:"options"`
	} `json:"chunker"`
}

func (s *IndexService) CreateBuild(ctx context.Context, r *indexingv1.CreateBuildRequest) (*indexingv1.JSONResponse, error) {
	if e := s.enabled(); e != nil {
		return nil, e
	}
	if e := validID(r.GetIndexId()); e != nil {
		return nil, e
	}
	if e := keyRequired(r.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	config, configError := indexing.ParseHNSW(r.GetHnswJson())
	if configError != nil {
		return nil, invalid(configError.Error())
	}
	if (r.ChunkingRunId == "") == (len(r.ChunkingRequestJson) == 0) {
		return nil, invalid("choose exactly one completed chunking_run_id or new chunking request")
	}
	for _, id := range []string{r.ChunkingRunId, r.ParentVersionId, r.EmbeddingDeploymentId} {
		if id != "" {
			if e := validID(id); e != nil {
				return nil, e
			}
		}
	}
	ids := append([]string{}, r.CanonicalIds...)
	sort.Strings(ids)
	if len(ids) > 20 {
		return nil, invalid("at most 20 canonical IDs")
	}
	for i, id := range ids {
		if e := validID(id); e != nil {
			return nil, e
		}
		if i > 0 && ids[i-1] == id {
			return nil, invalid("duplicate canonical ID")
		}
	}
	var newRequest newChunkRequest
	if len(r.ChunkingRequestJson) > 0 {
		if len(ids) > 0 || chunking.Decode(r.ChunkingRequestJson, &newRequest) != nil {
			return nil, invalid("new chunking input cannot have an additional subset")
		}
	}
	fp := chunking.HashJSON(struct {
		Version, Index, Run, Parent, Deployment string
		IDs                                     []string
		New                                     newChunkRequest
	}{"index-build-v1", r.IndexId, r.ChunkingRunId, r.ParentVersionId, r.EmbeddingDeploymentId, ids, newRequest})
	// Preserve the original fingerprint for omitted AND explicit default settings.
	if config != indexing.DefaultConfig() {
		fp = chunking.HashJSON(struct {
			Version, Base string
			Config        indexing.Config
		}{"index-build-hnsw-v1", fp, config})
	}
	out := &indexingv1.JSONResponse{}
	e := idempotency.ExecuteWithTimeout(ctx, s.chunks.pool, "index.build", r.IdempotencyKey, fp, out, 90*time.Second, func(ctx context.Context, tx pgx.Tx) error {
		idx, e := scanIndex(tx.QueryRow(ctx, indexSelect+` WHERE index_id=$1 FOR UPDATE`, r.IndexId))
		if e != nil {
			return indexError(e)
		}
		if idx.State != "active" {
			return precondition("INDEX_DELETING: cannot build")
		}
		if r.ParentVersionId != "" {
			p, e := scanVersion(tx.QueryRow(ctx, versionSelect+` WHERE index_version_id=$1`, r.ParentVersionId))
			if e != nil {
				return indexError(e)
			}
			if p.IndexID != idx.ID || p.State != "ready" {
				return precondition("parent must be a ready version of this index")
			}
		}
		deployment := r.EmbeddingDeploymentId
		if deployment == "" {
			deployment = idx.DeploymentID
		}
		c, cancel := context.WithTimeout(ctx, 20*time.Second)
		profile, e := s.model.GetProfile(c, &embeddingv1.GetProfileRequest{DeploymentId: deployment})
		cancel()
		if e != nil {
			return e
		}
		if e = indexing.ValidateProfile(profile); e != nil || profile.GetDeploymentId() != deployment {
			return precondition("EMBEDDING_PROFILE_INVALID: model profile unavailable or identity differs")
		}
		v := indexing.Version{SchemaVersion: indexing.SchemaVersion, ID: uuid.NewString(), IndexID: idx.ID, OperationID: uuid.NewString(), ParentID: r.ParentVersionId, ChunkingRunID: r.ChunkingRunId, Profile: profile, IndexingConfig: &config, State: "protecting", ReferenceState: "pending"}
		if v.ChunkingRunID != "" {
			run, e := scan(tx.QueryRow(ctx, runSelect+` WHERE chunking_run_id=$1`, v.ChunkingRunID))
			if e != nil {
				return indexError(e)
			}
			if run.ResultSha256 == "" {
				return precondition("CHUNK_SET_NOT_READY: choose a completed chunking run")
			}
			if chunking.Decode(run.SpecJson, &v.Spec) != nil {
				return corrupt("stored chunk spec is invalid")
			}
			v.ChunkSHA = run.ResultSha256
		} else {
			req := &chunkingv1.CreateRunRequest{Name: newRequest.Name, CanonicalIds: newRequest.CanonicalIDs, ChunkerId: newRequest.Chunker.ID, OptionsJson: newRequest.Chunker.Options, IdempotencyKey: "build-child-" + v.ID}
			name, docIDs, selection, _, e := normalize(req)
			if e != nil {
				return e
			}
			inputs := []chunking.Input{}
			for _, id := range docIDs {
				in, e := s.chunks.inputs.Resolve(ctx, id)
				if e != nil {
					return e
				}
				inputs = append(inputs, in)
			}
			v.Spec, e = chunking.NewSpec(inputs, selection)
			if e != nil {
				return precondition(e.Error())
			}
			v.ChunkingRunID = uuid.NewString()
			childOp := uuid.NewString()
			raw, _ := json.Marshal(v.Spec)
			if _, e = tx.Exec(ctx, `INSERT INTO chunking_runs(chunking_run_id,operation_id,name,spec_json) VALUES($1,$2,$3,$4)`, v.ChunkingRunID, childOp, name, raw); e != nil {
				return e
			}
			if e = submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: childOp, Kind: "chunking.run", ExecutionType: "chunking.run", ResourceType: "chunking_run", ResourceId: v.ChunkingRunID, MaxAttempts: 1}); e != nil {
				return e
			}
		}
		allowed := map[string]bool{}
		for _, in := range v.Spec.Documents {
			allowed[in.CanonicalID] = true
		}
		v.CanonicalIDs = append([]string{}, ids...)
		if len(v.CanonicalIDs) == 0 {
			for _, in := range v.Spec.Documents {
				v.CanonicalIDs = append(v.CanonicalIDs, in.CanonicalID)
			}
		}
		for _, id := range v.CanonicalIDs {
			if !allowed[id] {
				return invalid("subset contains a canonical ID outside the ChunkSet")
			}
		}
		raw, _ := json.Marshal(v)
		if _, e = tx.Exec(ctx, `INSERT INTO index_versions(index_version_id,index_id,operation_id,spec_json,state) VALUES($1,$2,$3,$4,'protecting')`, v.ID, v.IndexID, v.OperationID, raw); e != nil {
			return e
		}
		if _, e = tx.Exec(ctx, `UPDATE logical_indexes SET revision=revision+1 WHERE index_id=$1`, idx.ID); e != nil {
			return e
		}
		out.Json = jsonIndex(v).Json
		return nil
	})
	if e != nil {
		return out, e
	}
	v, e := versionFrom(out.Json)
	if e != nil {
		return nil, corrupt("stored build response is invalid")
	}
	return jsonIndex(v), nil
}
func (s *IndexService) loadChunks(ctx context.Context, v indexing.Version) ([]chunking.Chunk, error) {
	select {
	case s.chunks.reads <- struct{}{}:
		defer func() { <-s.chunks.reads }()
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	run, e := s.chunks.GetRun(ctx, &chunkingv1.GetRunRequest{ChunkingRunId: v.ChunkingRunID})
	if e != nil {
		return nil, e
	}
	if run.ResultSha256 == "" || v.ChunkSHA != "" && run.ResultSha256 != v.ChunkSHA {
		return nil, precondition("CHUNK_SET_NOT_READY_OR_CHANGED")
	}
	key, _ := blob.Key(run.ChunkingRunId, run.ResultSha256)
	raw, e := blob.Read(ctx, s.chunks.store, key, run.ResultSha256, run.ResultSizeBytes)
	if e != nil {
		return nil, corrupt("chunk artifact integrity failed")
	}
	result, e := chunking.Verify(raw, v.Spec)
	if e != nil {
		return nil, corrupt("chunk artifact does not match pinned input")
	}
	return indexing.Select(*result, v.CanonicalIDs), nil
}
