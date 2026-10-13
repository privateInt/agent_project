package app

import (
	"context"
	"encoding/json"
	"errors"
	"time"

	indexingv1 "knowledge-platform/gen/indexing/v1"
	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	"knowledge-platform/internal/idempotency"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
	"knowledge-platform/internal/searchstore"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Kept separate from the write lifecycle interface so search does not require
// a Model client when rerank is off or reference mutations.
type VectorSearchStore interface {
	Search(context.Context, indexing.Version, retrieval.Query) (retrieval.Candidates, error)
}

func searchInputError(err error) error {
	var input *retrieval.Error
	if errors.As(err, &input) {
		switch input.Code {
		case "SEARCH_SPACE_MISMATCH", "SEARCH_RERANK_PROFILE_INVALID", "RERANK_PROFILE_MISMATCH": return precondition(err.Error())
		case "SEARCH_CATALOG_INVALID", "SEARCH_RESPONSE_INVALID", "SEARCH_RERANK_RESPONSE_INVALID": return corrupt(err.Error())
		case "SEARCH_REQUEST_TOO_LARGE": return status.Error(codes.ResourceExhausted, err.Error())
		default: return invalid(err.Error())
		}
	}
	return corrupt("SEARCH_CATALOG_INVALID: search validation failed")
}
func searchStorageError(ctx context.Context, err error) error {
	if ctx.Err() != nil { return status.FromContextError(ctx.Err()).Err() }
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, searchstore.ErrSearchTimeout) {
		return status.Error(codes.DeadlineExceeded, "SEARCH_TIMEOUT: search did not complete")
	}
	if errors.Is(err, context.Canceled) { return status.FromContextError(err).Err() }
	if errors.Is(err, searchstore.ErrResponseTooLarge) {
		return status.Error(codes.ResourceExhausted, "SEARCH_RESPONSE_TOO_LARGE: reduce candidate_k and top_k")
	}
	if errors.Is(err, searchstore.ErrInvalidSearchResponse) || errors.Is(err, searchstore.ErrInvalidResponse) { return corrupt("SEARCH_RESPONSE_INVALID: backend result could not be validated") }
	if errors.Is(err, searchstore.ErrMissingIndex) { return unavailable(err.Error()) }
	return unavailable("SEARCH_BACKEND_UNCONFIRMED: backend search could not be verified")
}

func (s *IndexService) Search(ctx context.Context, r *indexingv1.SearchRequest) (*indexingv1.JSONResponse, error) {
	started := time.Now()
	if r == nil || !retrieval.ValidID(r.GetIndexId()) { return nil, invalid("SEARCH_INVALID_REQUEST: index UUID is required") }
	q, err := retrieval.Parse(r.GetQueryJson())
	if err != nil { return nil, searchInputError(err) }
	budget:=15*time.Second;if q.Rerank.Enabled{budget=60*time.Second}
	ctx, cancel := context.WithDeadline(ctx, started.Add(budget))
	defer cancel()
	backend, ok := s.search.(VectorSearchStore)
	if !ok { return nil, unavailable("SEARCH_NOT_CONFIGURED: configure the Knowledge OpenSearch connection") }
	select {
	case s.searchReads <- struct{}{}: defer func() { <-s.searchReads }()
	case <-ctx.Done(): return nil, status.FromContextError(ctx.Err()).Err()
	}
	tx, err := s.chunks.pool.Begin(ctx)
	if err != nil { return nil, searchDatabaseError(ctx, err) }
	defer idempotency.Rollback(tx)
	// All catalog writers lock parent -> version. Concurrent searches share these
	// locks; deletion and activation wait for bounded in-flight searches.
	idx, err := scanIndex(tx.QueryRow(ctx, indexSelect+` WHERE index_id=$1 FOR SHARE`, r.IndexId))
	if err != nil { return nil, searchDatabaseError(ctx, err) }
	if idx.State != "active" { return nil, precondition("SEARCH_INDEX_UNAVAILABLE: index is deleting or deleted") }
	v, err := scanVersion(tx.QueryRow(ctx, versionSelect+` WHERE index_version_id=$1 AND index_id=$2 FOR SHARE`, q.VersionID, idx.ID))
	if err != nil { return nil, searchDatabaseError(ctx, err) }
	if v.ID != q.VersionID || v.IndexID != idx.ID { return nil, corrupt("SEARCH_CATALOG_INVALID: stored version identity differs") }
	if v.State != "ready" || v.ReferenceState != "held" || v.Count < 1 || v.Count != v.Next {
		return nil, precondition("SEARCH_VERSION_UNAVAILABLE: version must be ready with held references")
	}
	if err = retrieval.Validate(v, q); err != nil { return nil, searchInputError(err) }
	timings:=retrieval.Timings{}
	var profile *rerankingv1.RerankerProfile
	if q.Rerank.Enabled {
		profileStarted:=time.Now()
		profile,err=s.rerankProfile(ctx,q)
		timings.ProfileMS=time.Since(profileStarted).Milliseconds()
		if err!=nil{return nil,err}
	}
	searchStarted := time.Now()
	candidates, err := backend.Search(ctx, v, q)
	if err != nil { return nil, searchStorageError(ctx, err) }
	timings.SearchMS=time.Since(searchStarted).Milliseconds();timings.BackendMS=candidates.BackendTookMS
	var response retrieval.Response
	if q.Rerank.Enabled {
		candidates,err=retrieval.OrderCandidates(q,candidates);if err!=nil{return nil,searchInputError(err)}
		rerankStarted:=time.Now()
		receipt,e:=s.rerankCandidates(ctx,q,candidates,profile);if e!=nil{return nil,e}
		timings.RerankMS=time.Since(rerankStarted).Milliseconds()
		response,err=retrieval.FinishReranked(idx.ID,q,candidates,profile,receipt,timings)
	} else { response,err=retrieval.Finish(idx.ID,q,candidates,timings) }
	if err != nil { return nil, searchInputError(err) }
	response.Timings.TotalMS=time.Since(started).Milliseconds()
	raw, err := json.Marshal(response)
	if err != nil { return nil, corrupt("SEARCH_RESPONSE_INVALID: result serialization failed") }
	if len(raw) > retrieval.MaxResponseBytes {
		return nil, status.Error(codes.ResourceExhausted, "SEARCH_RESPONSE_TOO_LARGE: reduce candidate_k and top_k")
	}
	if err = tx.Commit(ctx); err != nil { return nil, searchDatabaseError(ctx, err) }
	return &indexingv1.JSONResponse{Json: raw}, nil
}

func searchDatabaseError(ctx context.Context, err error) error {
	if ctx.Err() != nil { return status.FromContextError(ctx.Err()).Err() }
	return indexError(err)
}
