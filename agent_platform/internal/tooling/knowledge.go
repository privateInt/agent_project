package tooling

import (
	"agent-platform/internal/rpc"
	"encoding/json"
	"regexp"
	"strings"
	"unicode/utf8"
)

// KnowledgeBinding is immutable. Connection versions and the index version never
// follow a mutable current/active pointer after the Tool version is created.
type KnowledgeBinding struct {
	ModelConnectionVersionID string           `json:"model_connection_version_id"`
	IndexID                  string           `json:"index_id"`
	IndexVersionID           string           `json:"index_version_id"`
	EmbeddingSpaceID         string           `json:"embedding_space_id"`
	EmbeddingDeploymentID    string           `json:"embedding_deployment_id"`
	TopK                     int              `json:"top_k"`
	CandidateK               int              `json:"candidate_k"`
	Rerank                   *KnowledgeRerank `json:"rerank,omitempty"`
}
type KnowledgeRerank struct {
	DeploymentID      string `json:"deployment_id"`
	ExpectedProfileID string `json:"expected_reranker_profile_id"`
}
type KnowledgeInspect struct {
	Action                   string `json:"action"`
	ConnectionVersionID      string `json:"connection_version_id"`
	ModelConnectionVersionID string `json:"model_connection_version_id"`
	IndexID                  string `json:"index_id,omitempty"`
	IndexVersionID           string `json:"index_version_id,omitempty"`
	RerankDeploymentID       string `json:"rerank_deployment_id,omitempty"`
	Cursor                   string `json:"cursor,omitempty"`
}
type KnowledgeArguments struct {
	Query      string            `json:"query"`
	TopK       *int              `json:"top_k,omitempty"`
	CandidateK *int              `json:"candidate_k,omitempty"`
	Filters    *KnowledgeFilters `json:"filters,omitempty"`
}
type KnowledgeFilters struct {
	SourceIDs    []string `json:"source_document_ids,omitempty"`
	CanonicalIDs []string `json:"canonical_ids,omitempty"`
	Kinds        []string `json:"kinds,omitempty"`
}

var spaceID = regexp.MustCompile(`^es_[a-f0-9]{64}$`)
var knowledgeCursor = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
var rerankerID = regexp.MustCompile(`^rp_[a-f0-9]{64}$`)

func ValidateKnowledgeInspect(in KnowledgeInspect) error {
	if !rpc.ValidID(in.ConnectionVersionID) || !rpc.ValidID(in.ModelConnectionVersionID) {
		return E("TOOL_INPUT_INVALID")
	}
	switch in.Action {
	case "indexes":
		if in.IndexID != "" || in.IndexVersionID != "" || in.RerankDeploymentID != "" || (in.Cursor != "" && (len(in.Cursor) > 2048 || !knowledgeCursor.MatchString(in.Cursor))) {
			return E("TOOL_INPUT_INVALID")
		}
	case "resolve":
		if !rpc.ValidID(in.IndexID) || in.Cursor != "" || (in.IndexVersionID != "" && !rpc.ValidID(in.IndexVersionID)) || (in.RerankDeploymentID != "" && !rpc.ValidID(in.RerankDeploymentID)) {
			return E("TOOL_INPUT_INVALID")
		}
	default:
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func validateKnowledgeVersion(v VersionCreate) error {
	b, k := v.Binding, v.Binding.Knowledge
	if v.ExpectedRevision < 1 || v.TimeoutMS < 1000 || v.TimeoutMS > 240000 || k == nil || !rpc.ValidID(b.ConnectionVersionID) || b.ImportID != "" || b.OperationKey != "" || b.AuthScheme != "" || b.Effect != "" || b.ForwardIdempotency || b.HTTP != nil || b.Prefix != "" || b.DelayMS != 0 || b.Fail {
		return E("TOOL_INPUT_INVALID")
	}
	if !rpc.ValidID(k.ModelConnectionVersionID) || !rpc.ValidID(k.IndexID) || !rpc.ValidID(k.IndexVersionID) || !rpc.ValidID(k.EmbeddingDeploymentID) || !spaceID.MatchString(k.EmbeddingSpaceID) || k.TopK < 1 || k.TopK > 50 || k.CandidateK < k.TopK || k.CandidateK > 100 {
		return E("TOOL_INPUT_INVALID")
	}
	if k.Rerank != nil && (!rpc.ValidID(k.Rerank.DeploymentID) || !rerankerID.MatchString(k.Rerank.ExpectedProfileID)) {
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func validateKnowledgeArguments(v ToolVersion, raw []byte) error {
	var in KnowledgeArguments
	if err := StrictJSON(raw, &in); err != nil {
		return err
	}
	k := v.Binding.Knowledge
	if k == nil || !utf8.ValidString(in.Query) || strings.TrimSpace(in.Query) == "" || len(in.Query) > 8192 || strings.ContainsRune(in.Query, '\x00') {
		return E("TOOL_INPUT_INVALID")
	}
	top, candidate := k.TopK, k.CandidateK
	if in.TopK != nil {
		top = *in.TopK
	}
	candidate = max(candidate, top)
	if in.CandidateK != nil {
		candidate = *in.CandidateK
	}
	if top < 1 || top > 50 || candidate < top || candidate > 100 {
		return E("TOOL_INPUT_INVALID")
	}
	if in.Filters != nil {
		for _, ids := range [][]string{in.Filters.SourceIDs, in.Filters.CanonicalIDs, in.Filters.Kinds} {
			limit := 20
			if len(ids) == 0 && ids != nil {
				return E("TOOL_INPUT_INVALID")
			}
			if len(ids) > limit {
				return E("TOOL_INPUT_INVALID")
			}
			seen := map[string]bool{}
			for _, id := range ids {
				if seen[id] {
					return E("TOOL_INPUT_INVALID")
				}
				seen[id] = true
			}
		}
		for _, ids := range [][]string{in.Filters.SourceIDs, in.Filters.CanonicalIDs} {
			for _, id := range ids {
				if !rpc.ValidID(id) {
					return E("TOOL_INPUT_INVALID")
				}
			}
		}
		if len(in.Filters.Kinds) > 3 {
			return E("TOOL_INPUT_INVALID")
		}
		for _, kind := range in.Filters.Kinds {
			if kind != "text" && kind != "table" && kind != "code" {
				return E("TOOL_INPUT_INVALID")
			}
		}
	}
	return nil
}
func knowledgeSchemas() (json.RawMessage, json.RawMessage, error) {
	return json.RawMessage(`{"type":"object","additionalProperties":false,"required":["query"],"properties":{"query":{"type":"string","title":"질문","description":"검색할 질문을 자연어로 입력하세요. 최대 8 KiB입니다.","minLength":1,"maxLength":8192},"top_k":{"type":"integer","title":"결과 개수","minimum":1,"maximum":50},"candidate_k":{"type":"integer","title":"검색 후보 개수","minimum":1,"maximum":100},"filters":{"type":"object","additionalProperties":false,"properties":{"source_document_ids":{"type":"array","minItems":1,"maxItems":20,"uniqueItems":true,"items":{"type":"string","format":"uuid"}},"canonical_ids":{"type":"array","minItems":1,"maxItems":20,"uniqueItems":true,"items":{"type":"string","format":"uuid"}},"kinds":{"type":"array","minItems":1,"maxItems":3,"uniqueItems":true,"items":{"type":"string","enum":["text","table","code"]}}}}}}`), json.RawMessage(`{"type":"object","required":["index_id","index_version_id","embedding_space_id","returned_count","results"],"properties":{"index_id":{"type":"string","format":"uuid"},"index_version_id":{"type":"string","format":"uuid"},"embedding_space_id":{"type":"string"},"returned_count":{"type":"integer","minimum":0},"results":{"type":"array","items":{"type":"object"}}},"additionalProperties":true}`), nil
}
func KnowledgeStage(stage string) bool {
	switch stage {
	case "input", "index", "embedding_profile", "rerank_profile", "embedding", "search":
		return true
	}
	return false
}
func KnowledgeError(code string) bool {
	switch code {
	case "KNOWLEDGE_NOT_READY", "KNOWLEDGE_PROFILE_MISMATCH", "KNOWLEDGE_RESOURCE_NOT_FOUND", "TOOL_TIMEOUT", "TOOL_UPSTREAM_UNAVAILABLE", "TOOL_TARGET_BLOCKED", "TOOL_RESPONSE_INVALID", "TOOL_RESPONSE_TOO_LARGE", "TOOL_HTTP_ERROR", "CREDENTIAL_NOT_CONFIGURED", "CREDENTIAL_REVISION_UNAVAILABLE", "TOOL_INPUT_INVALID":
		return true
	}
	return false
}
