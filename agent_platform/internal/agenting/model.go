// Package agenting owns the fixed knowledge QA Agent contract.
package agenting

import (
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"encoding/json"
	"strings"
	"time"
	"unicode/utf8"
)

const Template = "knowledge_qa_v1"

var Steps = []string{"validate", "search", "context", "generate", "finalize"}

const DefaultPrompt = "선택한 자료에 근거해 질문에 한국어로 명확하게 답하세요. 자료에 없는 내용은 추측하지 마세요."

type Agent struct {
	AgentID     string    `json:"agent_id"`
	DisplayName string    `json:"display_name"`
	Description string    `json:"description"`
	Enabled     bool      `json:"enabled"`
	Revision    int64     `json:"revision"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}
type Model struct {
	ConnectionVersionID string `json:"connection_version_id"`
	DeploymentID        string `json:"deployment_id"`
	ModelVersionID      string `json:"model_version_id"`
}
type Definition struct {
	TemplateID      string  `json:"template_id"`
	TemplateVersion int     `json:"template_version"`
	ToolVersionID   string  `json:"tool_version_id"`
	Model           Model   `json:"model"`
	Prompt          string  `json:"prompt"`
	ContextMaxChars int     `json:"context_max_chars"`
	MaxTokens       int     `json:"max_tokens"`
	Temperature     float64 `json:"temperature"`
	TimeoutMS       int     `json:"timeout_ms"`
}
type VersionCreate struct {
	ExpectedRevision int64 `json:"expected_revision"`
	Definition
}
type Version struct {
	AgentVersionID string `json:"agent_version_id"`
	AgentID        string `json:"agent_id"`
	Version        int    `json:"version"`
	Revision       int64  `json:"revision"`
	State          string `json:"state"`
	Definition
	InputSchema      json.RawMessage `json:"input_schema"`
	OutputSchema     json.RawMessage `json:"output_schema"`
	DefinitionSHA256 string          `json:"definition_sha256"`
	CreatedAt        time.Time       `json:"created_at"`
}
type Input struct {
	Question string          `json:"question"`
	Filters  json.RawMessage `json:"filters,omitempty"`
}
type RunCreate struct {
	AgentVersionID string                `json:"agent_version_id"`
	Input          Input                 `json:"input"`
	Mode           string                `json:"mode"`
	CallerContext  tooling.CallerContext `json:"caller_context,omitempty"`
}
type Snapshot struct {
	Version         Version                   `json:"version"`
	Tool            tooling.Snapshot          `json:"tool"`
	ModelConnection tooling.ConnectionVersion `json:"model_connection"`
	CallerContext   tooling.CallerContext     `json:"caller_context"`
}
type Accepted struct {
	AgentRunID     string `json:"agent_run_id"`
	AgentVersionID string `json:"agent_version_id"`
	State          string `json:"state"`
	StatusURL      string `json:"status_url"`
}
type Run struct {
	QueueMS          int64                 `json:"queue_ms"`
	ExecutionMS      int64                 `json:"execution_ms"`
	AgentRunID       string                `json:"agent_run_id"`
	AgentVersionID   string                `json:"agent_version_id"`
	RequestID        string                `json:"request_id"`
	InvocationKey    string                `json:"invocation_key"`
	InputSHA256      string                `json:"input_sha256"`
	DefinitionSHA256 string                `json:"definition_sha256"`
	Input            Input                 `json:"input"`
	Mode             string                `json:"mode"`
	State            string                `json:"state"`
	CallerContext    tooling.CallerContext `json:"caller_context"`
	ToolCallID       *string               `json:"tool_call_id,omitempty"`
	Result           json.RawMessage       `json:"result,omitempty"`
	Error            *tooling.CallError    `json:"error,omitempty"`
	CreatedAt        time.Time             `json:"created_at"`
	StartedAt        *time.Time            `json:"started_at,omitempty"`
	CompletedAt      *time.Time            `json:"completed_at,omitempty"`
	DeadlineAt       time.Time             `json:"deadline_at"`
}
type Step struct {
	DurationMS  int64           `json:"duration_ms"`
	StepID      string          `json:"step_id"`
	Position    int             `json:"position"`
	Attempt     int             `json:"attempt"`
	State       string          `json:"state"`
	StartedAt   *time.Time      `json:"started_at,omitempty"`
	CompletedAt *time.Time      `json:"completed_at,omitempty"`
	Detail      json.RawMessage `json:"detail"`
}
type Inspection struct {
	Action              string `json:"action"`
	ConnectionVersionID string `json:"connection_version_id"`
	DeploymentID        string `json:"deployment_id,omitempty"`
	Cursor              string `json:"cursor,omitempty"`
}

func Defaults(v *VersionCreate) {
	if v.TemplateID == "" {
		v.TemplateID = Template
	}
	if v.TemplateVersion == 0 {
		v.TemplateVersion = 1
	}
	if v.Prompt == "" {
		v.Prompt = DefaultPrompt
	}
	if v.ContextMaxChars == 0 {
		v.ContextMaxChars = 8000
	}
	if v.MaxTokens == 0 {
		v.MaxTokens = 512
	}
	if v.TimeoutMS == 0 {
		v.TimeoutMS = 300000
	}
}
func ValidateVersion(v VersionCreate) error {
	if v.ExpectedRevision < 1 || v.TemplateID != Template || v.TemplateVersion != 1 || !rpc.ValidID(v.ToolVersionID) || !rpc.ValidID(v.Model.ConnectionVersionID) || !rpc.ValidID(v.Model.DeploymentID) || !rpc.ValidID(v.Model.ModelVersionID) || !utf8.ValidString(v.Prompt) || strings.TrimSpace(v.Prompt) == "" || len(v.Prompt) > 4000 || strings.ContainsRune(v.Prompt, 0) || v.ContextMaxChars < 256 || v.ContextMaxChars > 12000 || v.MaxTokens < 1 || v.MaxTokens > 512 || v.Temperature < 0 || v.Temperature > 1.5 || v.TimeoutMS < 1000 || v.TimeoutMS > 600000 {
		return tooling.E("AGENT_INPUT_INVALID")
	}
	return nil
}
func Arguments(in Input) json.RawMessage {
	a := map[string]any{"query": in.Question}
	if len(in.Filters) > 0 {
		a["filters"] = in.Filters
	}
	return tooling.JSON(a)
}
func ValidateInput(in Input, tool tooling.ToolVersion) error {
	if len(in.Question) > 8192 || strings.TrimSpace(in.Question) == "" || !utf8.ValidString(in.Question) || strings.ContainsRune(in.Question, 0) {
		return tooling.E("AGENT_INPUT_INVALID")
	}
	if tooling.ValidateArguments(tool, Arguments(in)) != nil {
		return tooling.E("AGENT_INPUT_INVALID")
	}
	return nil
}
func Stage(s string) bool {
	for _, v := range Steps {
		if v == s {
			return true
		}
	}
	return s == "queue" || s == "execute"
}
func ErrorCode(s string) bool {
	switch s {
	case "WORKFLOW_LINK_INVALID", "WORKFLOW_LINK_NOT_FOUND", "WORKFLOW_LINK_CONFLICT", "AGENT_INPUT_INVALID", "AGENT_NOT_FOUND", "AGENT_RUN_NOT_FOUND", "AGENT_VERSION_UNAVAILABLE", "AGENT_TEST_REQUIRED", "AGENT_LIMIT_REACHED", "AGENT_EXECUTION_FENCED", "AGENT_EXECUTION_INTERRUPTED", "AGENT_TIMEOUT", "AGENT_SEARCH_FAILED", "AGENT_RESULT_INVALID", "MODEL_NOT_READY", "MODEL_IDENTITY_MISMATCH", "MODEL_NOT_FOUND", "MODEL_INPUT_REJECTED", "MODEL_RESPONSE_INVALID", "MODEL_UNAVAILABLE", "MODEL_TIMEOUT", "CONTEXT_TOO_LARGE", "INPUT_TOO_LARGE", "ANSWER_REFERENCE_INVALID":
		return true
	}
	return false
}
func InputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","properties":{"question":{"type":"string","title":"질문","minLength":1,"maxLength":8192},"filters":{"type":"object","properties":{"source_document_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":20,"uniqueItems":true},"canonical_ids":{"type":"array","items":{"type":"string"},"minItems":1,"maxItems":20,"uniqueItems":true},"kinds":{"type":"array","items":{"type":"string","enum":["text","table","code"]},"minItems":1,"maxItems":3,"uniqueItems":true}},"additionalProperties":false}},"required":["question"],"additionalProperties":false}`)
}
func OutputSchema() json.RawMessage {
	return json.RawMessage(`{"type":"object","required":["answer","outcome","citations","warnings","answer_truncated","tool_call_id","identity"],"properties":{"answer":{"type":"string"},"outcome":{"enum":["answered","no_evidence"]},"citations":{"type":"array"},"warnings":{"type":"array"},"answer_truncated":{"type":"boolean"},"tool_call_id":{"type":"string"},"identity":{"type":"object"}}}`)
}
