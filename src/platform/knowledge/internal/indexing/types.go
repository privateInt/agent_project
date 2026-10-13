// Package indexing defines immutable build inputs and validates Model receipts.
package indexing

import (
	"encoding/json"
	"fmt"
	"google.golang.org/protobuf/proto"
	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	"knowledge-platform/internal/chunking"
	"math"
	"strings"
)

const SchemaVersion = "index-version-v1"
const MaxBatch = 16

type Index struct {
	ID              string `json:"index_id"`
	Name            string `json:"name"`
	DeploymentID    string `json:"embedding_deployment_id"`
	ActiveVersionID string `json:"active_version_id"`
	Revision        int64  `json:"revision"`
	State           string `json:"state"`
	CreatedAt       string `json:"created_at"`
}
type Version struct {
	SchemaVersion  string                        `json:"schema_version"`
	ID             string                        `json:"index_version_id"`
	IndexID        string                        `json:"index_id"`
	OperationID    string                        `json:"operation_id"`
	ParentID       string                        `json:"parent_version_id"`
	ChunkingRunID  string                        `json:"chunking_run_id"`
	ChunkSHA       string                        `json:"chunk_set_sha256"`
	Spec           chunking.Spec                 `json:"input_spec"`
	CanonicalIDs   []string                      `json:"canonical_ids"`
	Profile        *embeddingv1.EmbeddingProfile `json:"embedding_profile"`
	IndexingConfig *Config                       `json:"indexing_config,omitempty"`
	State          string                        `json:"state"`
	Count          int                           `json:"chunk_count"`
	Next           int                           `json:"indexed_count"`
	ReferenceState string                        `json:"reference_state"`
	ErrorCode      string                        `json:"error_code"`
	ErrorMessage   string                        `json:"error_message"`
	CreatedAt      string                        `json:"created_at"`
}
type Batch struct {
	Version Version          `json:"version"`
	Offset  int              `json:"offset"`
	Chunks  []chunking.Chunk `json:"chunks"`
}
type Record struct {
	ID             string         `json:"chunk_id"`
	IndexVersionID string         `json:"index_version_id"`
	SpaceID        string         `json:"embedding_space_id"`
	SourceID       string         `json:"source_document_id"`
	CanonicalID    string         `json:"canonical_id"`
	Kind           string         `json:"kind"`
	Ordinal        int            `json:"ordinal"`
	Text           string         `json:"text"`
	ContentSHA     string         `json:"content_sha256"`
	Vector         []float64      `json:"vector"`
	Provenance     chunking.Chunk `json:"provenance"`
	InputTokens    int32          `json:"input_tokens"`
}

func ValidateProfile(p *embeddingv1.EmbeddingProfile) error {
	if p == nil || p.Capability != "embedding" || p.Status != "ready" || p.ProfileId != "snowflake-ko-v1" || p.RepoId != "dragonkue/snowflake-arctic-embed-l-v2.0-ko" || p.QueryPrefix != "query: " || p.DocumentPrefix != "" || p.Dimension != 1024 || p.Normalization != "l2" || p.Pooling != "cls" || p.Truncation != "reject" || p.MaxInputTokens < 128 || p.MaxInputTokens > 8192 || p.MaxBatchSize < 1 || p.MaxBatchSize > 16 || p.MaxInputBytes < 1 || p.MaxInputBytes > 131072 || !strings.HasPrefix(p.EmbeddingSpaceId, "es_") || !chunking.ValidDigest(strings.TrimPrefix(p.EmbeddingSpaceId, "es_")) || !chunking.ValidDigest(p.SourceManifestSha256) || !chunking.ValidDigest(p.TokenizerSha256) || (!strings.HasPrefix(p.RuntimeImageId, "sha256:") || !chunking.ValidDigest(strings.TrimPrefix(p.RuntimeImageId, "sha256:"))) || !chunking.ValidDigest(p.RuntimeContainerId) || p.DeploymentId == "" || p.ModelVersionId == "" || p.ServedModelName != "deployment-"+p.DeploymentId {
		return fmt.Errorf("EMBEDDING_PROFILE_INVALID: unsupported or incomplete Model profile")
	}
	return nil
}
func ValidateResult(p *embeddingv1.EmbeddingProfile, chunks []chunking.Chunk, result *embeddingv1.EmbedResponse) error {
	if e := ValidateProfile(p); e != nil {
		return e
	}
	if result == nil || !proto.Equal(result.Profile, p) || result.Role != "document" || len(result.Data) != len(chunks) || len(chunks) < 1 || len(chunks) > int(p.MaxBatchSize) {
		return fmt.Errorf("EMBEDDING_RECEIPT_MISMATCH: Model identity, role or count changed")
	}
	for i, row := range result.Data {
		if row == nil || row.Id != chunks[i].ID || row.Index != int32(i) || len(row.Embedding) != int(p.Dimension) || row.InputTokens < 1 || row.InputTokens > p.MaxInputTokens {
			return fmt.Errorf("EMBEDDING_RECEIPT_MISMATCH: ID, position, dimension or tokens")
		}
		norm := 0.0
		for _, n := range row.Embedding {
			if math.IsNaN(n) || math.IsInf(n, 0) {
				return fmt.Errorf("EMBEDDING_VECTOR_INVALID: nonfinite value")
			}
			norm += n * n
		}
		if math.IsInf(norm, 0) || math.Abs(math.Sqrt(norm)-1) > 0.001 {
			return fmt.Errorf("EMBEDDING_VECTOR_INVALID: expected unit vector")
		}
	}
	return nil
}
func Select(result chunking.Result, ids []string) []chunking.Chunk {
	set := map[string]bool{}
	for _, id := range ids {
		set[id] = true
	}
	out := []chunking.Chunk{}
	for _, c := range result.Chunks {
		if set[c.CanonicalID] {
			out = append(out, c)
		}
	}
	return out
}
func Slice(chunks []chunking.Chunk, offset int, p *embeddingv1.EmbeddingProfile) ([]chunking.Chunk, error) {
	if offset < 0 || offset > len(chunks) {
		return nil, fmt.Errorf("invalid batch offset")
	}
	end := offset
	size := 0
	for end < len(chunks) && end-offset < int(p.MaxBatchSize) && end-offset < MaxBatch {
		n := len(chunks[end].Text)
		if size+n > int(p.MaxInputBytes) {
			break
		}
		raw, _ := json.Marshal(chunks[offset : end+1])
		if len(raw) > 1536<<10 {
			break
		}
		size += n
		end++
	}
	if end == offset && offset < len(chunks) {
		return nil, fmt.Errorf("CHUNK_TOO_LARGE: chunk exceeds embedding byte limit")
	}
	return chunks[offset:end], nil
}
func Records(v Version, chunks []chunking.Chunk, result *embeddingv1.EmbedResponse) []Record {
	out := make([]Record, len(chunks))
	for i, c := range chunks {
		out[i] = Record{c.ID, v.ID, v.Profile.EmbeddingSpaceId, c.SourceDocumentID, c.CanonicalID, c.Kind, c.Ordinal, c.Text, c.ContentSHA256, result.Data[i].Embedding, c, result.Data[i].InputTokens}
	}
	return out
}
func Receipt(result *embeddingv1.EmbedResponse) string {
	raw, _ := json.Marshal(result)
	return chunking.Digest(raw)
}
