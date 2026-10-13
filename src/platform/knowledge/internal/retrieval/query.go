// Package retrieval defines vector-search inputs without model or storage clients.
package retrieval

import (
	"bytes"
	"encoding/json"
	"fmt"
	"math"
	"sort"
	"strings"
	"unicode/utf8"

	"github.com/google/uuid"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
)

const MaxRequestBytes = 64 << 10
const MaxResponseBytes = 2 << 20

type Error struct { Code, Message string }
func (e *Error) Error() string { return e.Code + ": " + e.Message }
func invalid(code, message string) error { return &Error{code, message} }

type Filters struct {
	SourceIDs []string `json:"source_document_ids,omitempty"`
	CanonicalIDs []string `json:"canonical_ids,omitempty"`
	Kinds []string `json:"kinds,omitempty"`
}
type Query struct {
	VersionID string `json:"index_version_id"`
	SpaceID string `json:"embedding_space_id"`
	Vector []float64 `json:"query_embedding"`
	TopK int `json:"top_k"`
	CandidateK int `json:"candidate_k"`
	Filters Filters `json:"filters"`
	QueryText string `json:"query_text,omitempty"`
	Rerank RerankOptions `json:"rerank"`
}
type RerankOptions struct {
	Enabled bool `json:"enabled"`
	DeploymentID string `json:"deployment_id,omitempty"`
	ExpectedProfileID string `json:"expected_reranker_profile_id,omitempty"`
}
type Hit struct {
	chunking.Chunk
	Rank int `json:"rank"`
	RetrievalRank int `json:"retrieval_rank"`
	Score float64 `json:"retrieval_score"`
	RerankScore *float64 `json:"rerank_score,omitempty"`
	RerankInputTokens int32 `json:"rerank_input_tokens,omitempty"`
}
type Candidates struct {
	Hits []Hit
	BackendTookMS int64
}
type Rerank struct {
	Requested bool `json:"requested"`
	Applied bool `json:"applied"`
	Status string `json:"status"`
	DeploymentID string `json:"deployment_id,omitempty"`
	ProfileID string `json:"reranker_profile_id,omitempty"`
	Profile *RerankerProfile `json:"profile,omitempty"`
}
type Scores struct {
	ID string `json:"id"`
	HigherIsBetter bool `json:"higher_is_better"`
	Formula string `json:"formula"`
	IsProbability bool `json:"is_probability"`
	Approximate bool `json:"approximate"`
}
type Timings struct {
	SearchMS int64 `json:"search_ms"`
	BackendMS int64 `json:"backend_ms"`
	TotalMS int64 `json:"total_ms"`
	ProfileMS int64 `json:"profile_ms,omitempty"`
	RerankMS int64 `json:"rerank_ms,omitempty"`
}
type Response struct {
	IndexID string `json:"index_id"`
	VersionID string `json:"index_version_id"`
	SpaceID string `json:"embedding_space_id"`
	TopK int `json:"top_k"`
	CandidateK int `json:"candidate_k"`
	CandidateCount int `json:"candidate_count"`
	ReturnedCount int `json:"returned_count"`
	Filters Filters `json:"filters"`
	Rerank Rerank `json:"rerank"`
	Scores Scores `json:"score_semantics"`
	Timings Timings `json:"timings"`
	EmptyReason string `json:"empty_reason,omitempty"`
	Results []Hit `json:"results"`
}

func ValidID(s string) bool {
	id, err := uuid.Parse(s)
	return err == nil && id != uuid.Nil && id.String() == s
}
func object(raw []byte, allowed ...string) (map[string]json.RawMessage, error) {
	var fields map[string]json.RawMessage
	if chunking.Decode(raw, &fields) != nil || fields == nil {
		return nil, invalid("SEARCH_INVALID_REQUEST", "expected a JSON object without duplicate keys")
	}
	for key, value := range fields {
		found := false
		for _, name := range allowed { if key == name { found = true; break } }
		if !found || bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return nil, invalid("SEARCH_INVALID_REQUEST", "unknown field or null value")
		}
	}
	return fields, nil
}
func stringList(raw []byte, maximum int, ids bool) ([]string, error) {
	var items []string
	if json.Unmarshal(raw, &items) != nil || len(items) < 1 || len(items) > maximum {
		return nil, invalid("SEARCH_INVALID_FILTER", "filter arrays must be nonempty and within limits")
	}
	seen := map[string]bool{}
	for _, item := range items {
		if seen[item] || (ids && !ValidID(item)) || (!ids && item != "text" && item != "table" && item != "code") {
			return nil, invalid("SEARCH_INVALID_FILTER", "duplicate or unsupported filter value")
		}
		seen[item] = true
	}
	sort.Strings(items)
	return items, nil
}
func Parse(raw []byte) (Query, error) {
	q := Query{TopK: 5}
	if len(raw) > MaxRequestBytes { return q, invalid("SEARCH_REQUEST_TOO_LARGE", "request exceeds 64 KiB") }
	if !utf8.Valid(raw) { return q, invalid("SEARCH_INVALID_REQUEST", "request must be UTF-8") }
	f, err := object(raw, "index_version_id", "embedding_space_id", "query_embedding", "top_k", "candidate_k", "filters", "rerank", "query_text")
	if err != nil { return q, err }
	if json.Unmarshal(f["index_version_id"], &q.VersionID) != nil || !ValidID(q.VersionID) || json.Unmarshal(f["embedding_space_id"], &q.SpaceID) != nil || !strings.HasPrefix(q.SpaceID, "es_") || !chunking.ValidDigest(strings.TrimPrefix(q.SpaceID, "es_")) {
		return q, invalid("SEARCH_INVALID_REQUEST", "explicit version UUID and embedding space ID are required")
	}
	var vector []json.RawMessage
	if json.Unmarshal(f["query_embedding"], &vector) != nil || len(vector) != 1024 {
		return q, invalid("SEARCH_DIMENSION_MISMATCH", "expected one 1024-dimensional query vector")
	}
	q.Vector = make([]float64, len(vector))
	norm := 0.0
	for i, rawNumber := range vector {
		if bytes.Equal(bytes.TrimSpace(rawNumber), []byte("null")) || json.Unmarshal(rawNumber, &q.Vector[i]) != nil || math.IsNaN(q.Vector[i]) || math.IsInf(q.Vector[i], 0) {
			return q, invalid("SEARCH_VECTOR_INVALID", "vector elements must be finite numbers")
		}
		norm += q.Vector[i] * q.Vector[i]
	}
	if math.IsInf(norm, 0) || math.Abs(math.Sqrt(norm)-1) > 0.001 {
		return q, invalid("SEARCH_VECTOR_INVALID", "expected an L2-normalized nonzero vector")
	}
	if rawK, ok := f["top_k"]; ok {
		if json.Unmarshal(rawK, &q.TopK) != nil { return q, invalid("SEARCH_INVALID_REQUEST", "top_k must be an integer") }
	}
	q.CandidateK = q.TopK
	if rawK, ok := f["candidate_k"]; ok {
		if json.Unmarshal(rawK, &q.CandidateK) != nil { return q, invalid("SEARCH_INVALID_REQUEST", "candidate_k must be an integer") }
	}
	if q.TopK < 1 || q.TopK > 50 || q.CandidateK < q.TopK || q.CandidateK > 100 {
		return q, invalid("SEARCH_INVALID_REQUEST", "require 1 <= top_k <= 50 and top_k <= candidate_k <= 100")
	}
	if rawFilters, ok := f["filters"]; ok {
		filters, e := object(rawFilters, "source_document_ids", "canonical_ids", "kinds")
		if e != nil { return q, e }
		for _, field := range []struct { name string; target *[]string; maximum int; ids bool }{
			{"source_document_ids", &q.Filters.SourceIDs, 20, true},
			{"canonical_ids", &q.Filters.CanonicalIDs, 20, true},
			{"kinds", &q.Filters.Kinds, 3, false},
		} {
			if rawList, exists := filters[field.name]; exists {
				*field.target, err = stringList(rawList, field.maximum, field.ids)
				if err != nil { return q, err }
			}
		}
	}
	if rawRerank, ok := f["rerank"]; ok {
		rerank, e := object(rawRerank, "enabled", "deployment_id", "expected_reranker_profile_id")
		if e != nil || json.Unmarshal(rerank["enabled"], &q.Rerank.Enabled) != nil {
			return q, invalid("SEARCH_INVALID_REQUEST", "rerank requires an explicit boolean enabled")
		}
		if q.Rerank.Enabled {
			if len(rerank) != 3 || json.Unmarshal(rerank["deployment_id"], &q.Rerank.DeploymentID) != nil || !ValidID(q.Rerank.DeploymentID) || json.Unmarshal(rerank["expected_reranker_profile_id"], &q.Rerank.ExpectedProfileID) != nil || !ValidRerankerID(q.Rerank.ExpectedProfileID) || f["candidate_k"] == nil {
				return q, invalid("SEARCH_INVALID_REQUEST", "rerank requires deployment UUID, expected profile ID and explicit candidate_k")
			}
		} else if len(rerank) != 1 { return q, invalid("SEARCH_INVALID_REQUEST", "disabled rerank accepts no additional options") }
	}
	if q.Rerank.Enabled {
		if json.Unmarshal(f["query_text"], &q.QueryText) != nil || strings.TrimSpace(q.QueryText) == "" || len(q.QueryText) > 8<<10 || strings.ContainsRune(q.QueryText, 0) {
			return q, invalid("SEARCH_INVALID_REQUEST", "rerank requires raw query_text of 1..8192 UTF-8 bytes without NUL")
		}
	} else if f["query_text"] != nil { return q, invalid("SEARCH_INVALID_REQUEST", "query_text is only accepted with enabled rerank") }
	return q, nil
}

func contains(items []string, item string) bool {
	for _, value := range items { if item == value { return true } }
	return false
}
// Scope excludes canonical versions omitted when the index version was built.
func Scope(v indexing.Version, q Query) (map[string]chunking.Input, error) {
	allowed := map[string]chunking.Input{}
	for _, input := range v.Spec.Documents {
		if contains(v.CanonicalIDs, input.CanonicalID) { allowed[input.CanonicalID] = input }
	}
	if len(allowed) == 0 || len(allowed) != len(v.CanonicalIDs) {
		return nil, invalid("SEARCH_CATALOG_INVALID", "pinned document scope is invalid")
	}
	for _, id := range q.Filters.CanonicalIDs {
		if _, ok := allowed[id]; !ok { return nil, invalid("SEARCH_FILTER_OUTSIDE_VERSION", "canonical filter is outside this version") }
	}
	for _, id := range q.Filters.SourceIDs {
		found := false
		for _, input := range allowed { if input.SourceDocumentID == id { found = true; break } }
		if !found { return nil, invalid("SEARCH_FILTER_OUTSIDE_VERSION", "source filter is outside this version") }
	}
	selected := map[string]chunking.Input{}
	for id, input := range allowed {
		if len(q.Filters.CanonicalIDs) > 0 && !contains(q.Filters.CanonicalIDs, id) { continue }
		if len(q.Filters.SourceIDs) > 0 && !contains(q.Filters.SourceIDs, input.SourceDocumentID) { continue }
		selected[id] = input
	}
	if len(selected) == 0 { return nil, invalid("SEARCH_EMPTY_SCOPE", "document filter intersection is empty") }
	return selected, nil
}
func Validate(v indexing.Version, q Query) error {
	if v.ID != q.VersionID || indexing.ValidateProfile(v.Profile) != nil {
		return invalid("SEARCH_CATALOG_INVALID", "pinned version or embedding profile is invalid")
	}
	if v.Profile.EmbeddingSpaceId != q.SpaceID {
		return invalid("SEARCH_SPACE_MISMATCH", "query embedding space differs from the pinned version")
	}
	if len(q.Vector) != int(v.Profile.Dimension) { return invalid("SEARCH_DIMENSION_MISMATCH", "query dimension differs from the pinned version") }
	_, err := Scope(v, q)
	return err
}
func MatchChunk(c chunking.Chunk, scope map[string]chunking.Input, q Query) bool {
	input, ok := scope[c.CanonicalID]
	return ok && c.SourceDocumentID == input.SourceDocumentID && (len(q.Filters.Kinds) == 0 || contains(q.Filters.Kinds, c.Kind))
}
func Finish(indexID string, q Query, candidates Candidates, timings Timings) (Response, error) {
	if q.Rerank.Enabled { return Response{}, fmt.Errorf("SEARCH_RERANK_RESPONSE_INVALID: rerank receipt required") }
	if candidates.Hits == nil { candidates.Hits = []Hit{} }
	if len(candidates.Hits) > q.CandidateK { return Response{}, fmt.Errorf("SEARCH_RESPONSE_INVALID: excessive candidates") }
	sort.Slice(candidates.Hits, func(i, j int) bool {
		if candidates.Hits[i].Score == candidates.Hits[j].Score { return candidates.Hits[i].ID < candidates.Hits[j].ID }
		return candidates.Hits[i].Score > candidates.Hits[j].Score
	})
	for i := range candidates.Hits { candidates.Hits[i].Rank = i+1; candidates.Hits[i].RetrievalRank = i+1 }
	results := candidates.Hits[:min(q.TopK, len(candidates.Hits))]
	out := Response{
		IndexID: indexID, VersionID: q.VersionID, SpaceID: q.SpaceID,
		TopK: q.TopK, CandidateK: q.CandidateK, CandidateCount: len(candidates.Hits), ReturnedCount: len(results),
		Filters: q.Filters, Results: results, Rerank: Rerank{Status: "disabled"},
		Scores: Scores{ID: "opensearch_lucene_cosinesimil_v1", HigherIsBetter: true, Formula: "(1 + cosine_similarity) / 2", Approximate: true},
		Timings: timings,
	}
	if len(results) == 0 { out.EmptyReason = "no_matches" }
	return out, nil
}
