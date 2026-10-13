package searchstore

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"math"
	"unicode/utf8"

	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
)

var ErrInvalidSearchResponse = errors.New("SEARCH_RESPONSE_INVALID: backend result identity or content differs")
var ErrIncompleteSearch = errors.New("SEARCH_BACKEND_UNCONFIRMED: backend returned a partial result")
var ErrMissingIndex = errors.New("SEARCH_INDEX_MISSING: pinned physical index is unavailable")
var ErrResponseTooLarge = errors.New("OPENSEARCH_RESPONSE_TOO_LARGE")
var ErrInvalidResponse = errors.New("OPENSEARCH_INVALID_RESPONSE")
var ErrSearchTimeout = errors.New("SEARCH_TIMEOUT: backend search timed out")

func searchBody(v indexing.Version, q retrieval.Query) map[string]any {
	filters := []any{
		map[string]any{"term": map[string]any{"index_version_id": v.ID}},
		map[string]any{"term": map[string]any{"embedding_space_id": q.SpaceID}},
	}
	for _, field := range []struct { name string; values []string }{
		{"source_document_id", q.Filters.SourceIDs},
		{"canonical_id", q.Filters.CanonicalIDs},
		{"kind", q.Filters.Kinds},
	} {
		if len(field.values) > 0 { filters = append(filters, map[string]any{"terms": map[string]any{field.name: field.values}}) }
	}
	return map[string]any{
		"size": q.CandidateK, "timeout": "8s", "track_total_hits": false, "track_scores": true,
		"sort": []any{map[string]string{"_score": "desc"}, map[string]string{"chunk_id": "asc"}},
		"_source": []string{"chunk_id", "index_version_id", "embedding_space_id", "source_document_id", "canonical_id", "kind", "ordinal", "content_sha256", "provenance"},
		"query": map[string]any{"knn": map[string]any{"vector": map[string]any{
			"vector": q.Vector, "k": q.CandidateK, "filter": map[string]any{"bool": map[string]any{"filter": filters}},
		}}},
	}
}

// Search never creates an index or invokes a model. The service holds the
// catalog read locks for the lifetime of this call, including ownership checks.
func (c *Client) Search(ctx context.Context, v indexing.Version, q retrieval.Query) (retrieval.Candidates, error) {
	out := retrieval.Candidates{Hits: []retrieval.Hit{}}
	if err := retrieval.Validate(v, q); err != nil { return out, err }
	scope, err := retrieval.Scope(v, q)
	if err != nil { return out, err }
	exists, err := c.inspect(ctx, v)
	if err != nil { return out, err }
	if !exists { return out, ErrMissingIndex }
	name, _, err := names(v.ID)
	if err != nil { return out, err }
	body, err := json.Marshal(searchBody(v, q))
	if err != nil { return out, fmt.Errorf("SEARCH_INVALID_REQUEST: vector serialization failed") }
	var response struct {
		Took *int64 `json:"took"`
		TimedOut *bool `json:"timed_out"`
		TerminatedEarly bool `json:"terminated_early"`
		Shards *struct {
			Total *int `json:"total"`
			Successful *int `json:"successful"`
			Failed *int `json:"failed"`
		} `json:"_shards"`
		Hits *struct {
			Rows *[]struct {
				Index string `json:"_index"`
				ID string `json:"_id"`
				Score *float64 `json:"_score"`
				Source *indexing.Record `json:"_source"`
			} `json:"hits"`
		} `json:"hits"`
	}
	err = c.request(ctx, "POST", "/"+name+"/_search?allow_partial_search_results=false&ignore_unavailable=false&allow_no_indices=false", body, &response)
	if err != nil {
		if h, ok := err.(*HTTPError); ok && h.Code == 404 { return out, ErrMissingIndex }
		return out, err
	}
	if response.TimedOut == nil || response.Took == nil || *response.Took < 0 || response.Shards == nil || response.Shards.Total == nil || response.Shards.Successful == nil || response.Shards.Failed == nil || response.Hits == nil || response.Hits.Rows == nil {
		return out, ErrInvalidSearchResponse
	}
	if *response.TimedOut { return out, ErrSearchTimeout }
	if response.TerminatedEarly || *response.Shards.Total < 1 || *response.Shards.Successful != *response.Shards.Total || *response.Shards.Failed != 0 {
		return out, ErrIncompleteSearch
	}
	rows := *response.Hits.Rows
	if len(rows) > q.CandidateK { return out, ErrInvalidSearchResponse }
	seen := map[string]bool{}
	for _, row := range rows {
		if row.Index != name || row.Source == nil || row.Score == nil || math.IsNaN(*row.Score) || math.IsInf(*row.Score, 0) || *row.Score < 0 || *row.Score > 1.000001 || seen[row.ID] {
			return out, ErrInvalidSearchResponse
		}
		record := row.Source
		chunk := record.Provenance
		if !chunking.ValidDigest(row.ID) || record.ID != row.ID || record.IndexVersionID != v.ID || record.SpaceID != q.SpaceID || chunk.ID != row.ID || chunk.CanonicalID != record.CanonicalID || chunk.SourceDocumentID != record.SourceID || chunk.Kind != record.Kind || chunk.Ordinal != record.Ordinal || chunk.Ordinal < 0 || chunk.ContentSHA256 != record.ContentSHA || chunking.Digest([]byte(chunk.Text)) != chunk.ContentSHA256 || !utf8.ValidString(chunk.Text) || chunk.Text == "" || len(chunk.Spans) == 0 || len(chunk.Quality) == 0 || !json.Valid(chunk.Quality) || !retrieval.MatchChunk(chunk, scope, q) {
			return out, ErrInvalidSearchResponse
		}
		if chunk.Kind != "text" && chunk.Kind != "table" && chunk.Kind != "code" { return out, ErrInvalidSearchResponse }
		length := utf8.RuneCountInString(chunk.Text)
		if length > 8000 { return out, ErrInvalidSearchResponse }
		for _, span := range chunk.Spans {
			if span.BlockID == "" || span.Start < 0 || span.End <= span.Start || span.ChunkStart < 0 || span.ChunkEnd <= span.ChunkStart || span.ChunkEnd > length || span.End-span.Start != span.ChunkEnd-span.ChunkStart {
				return out, ErrInvalidSearchResponse
			}
		}
		seen[row.ID] = true
		out.Hits = append(out.Hits, retrieval.Hit{Chunk: chunk, Score: *row.Score})
	}
	out.BackendTookMS = *response.Took
	return out, nil
}
