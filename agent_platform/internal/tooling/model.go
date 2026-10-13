// Package tooling owns Agent Tool definitions and validation, shared by REST and MCP.
package tooling

import (
	"agent-platform/internal/apispec"
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/url"
	"regexp"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

const MaxInput = 256 << 10
const MaxOutput = 2 << 20
const Principal = "agent-api" // A03 has one authenticated deployment principal.
const MCPProtocol = "2025-11-25"

type Error struct{ Code string }

func (e *Error) Error() string { return e.Code }
func E(code string) error      { return &Error{Code: code} }
func Code(err error) string {
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	return "SERVICE_UNAVAILABLE"
}
func HTTPStatus(code string) int {
	switch code {
	case "WORKFLOW_LINK_INVALID", "AGENT_INPUT_INVALID", "INPUT_TOO_LARGE", "OPENAPI_INVALID", "OPENAPI_UNSUPPORTED", "TOOL_INPUT_INVALID", "INVALID_ARGUMENT", "IDEMPOTENCY_KEY_REQUIRED":
		return 400
	case "WORKFLOW_LINK_NOT_FOUND", "AGENT_NOT_FOUND", "AGENT_RUN_NOT_FOUND", "MODEL_NOT_FOUND", "KNOWLEDGE_RESOURCE_NOT_FOUND", "OPENAPI_NOT_FOUND", "TOOL_NOT_FOUND", "CONNECTION_NOT_FOUND", "TOOL_CALL_NOT_FOUND":
		return 404
	case "WORKFLOW_LINK_CONFLICT", "AGENT_VERSION_UNAVAILABLE", "AGENT_TEST_REQUIRED", "AGENT_LIMIT_REACHED", "MODEL_NOT_READY", "MODEL_IDENTITY_MISMATCH", "KNOWLEDGE_NOT_READY", "KNOWLEDGE_PROFILE_MISMATCH", "TOOL_TEST_REQUIRED", "TOOL_LIMIT_REACHED", "REVISION_CONFLICT", "KEY_CONFLICT", "TOOL_VERSION_UNAVAILABLE", "CONNECTION_UNAVAILABLE":
		return 409
	case "TOOL_PAYLOAD_TOO_LARGE":
		return 413
	case "MODEL_RESPONSE_INVALID", "TOOL_RESPONSE_INVALID", "TOOL_RESPONSE_TOO_LARGE", "TOOL_HTTP_ERROR":
		return 502
	case "MODEL_TIMEOUT", "AGENT_TIMEOUT", "TOOL_TIMEOUT", "TOOL_WAIT_TIMEOUT":
		return 504
	default:
		return 503
	}
}

// StrictJSON rejects duplicate keys, nulls, trailing values, deep nesting and
// invalid UTF-8 before decoding. JSON numbers remain exact until typed decoding.
func StrictJSON(raw []byte, out any) error              { return strictJSON(raw, out, MaxInput, false) }
func NullableJSON(raw []byte, out any, limit int) error { return strictJSON(raw, out, limit, true) }
func strictJSON(raw []byte, out any, limit int, allowNull bool) error {
	if len(raw) > limit {
		return E("TOOL_PAYLOAD_TOO_LARGE")
	}
	if !utf8.Valid(raw) {
		return E("TOOL_INPUT_INVALID")
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var walk func(int) error
	walk = func(depth int) error {
		if depth > 32 {
			return errors.New("depth")
		}
		token, err := dec.Token()
		if err != nil {
			return err
		}
		if token == nil && !allowNull {
			return errors.New("null")
		}
		delim, ok := token.(json.Delim)
		if !ok {
			return nil
		}
		switch delim {
		case '{':
			keys := map[string]bool{}
			for dec.More() {
				t, err := dec.Token()
				if err != nil {
					return err
				}
				k, ok := t.(string)
				if !ok || keys[k] {
					return errors.New("duplicate")
				}
				keys[k] = true
				if err = walk(depth + 1); err != nil {
					return err
				}
			}
		case '[':
			for dec.More() {
				if err := walk(depth + 1); err != nil {
					return err
				}
			}
		default:
			return errors.New("delimiter")
		}
		_, err = dec.Token()
		return err
	}
	if err := walk(0); err != nil {
		return E("TOOL_INPUT_INVALID")
	}
	if _, err := dec.Token(); err != io.EOF {
		return E("TOOL_INPUT_INVALID")
	}
	dec = json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	dec.UseNumber()
	if err := dec.Decode(out); err != nil {
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func Canonical(raw []byte) ([]byte, error) {
	var v any
	if err := StrictJSON(raw, &v); err != nil {
		return nil, err
	}
	return json.Marshal(v)
}
func JSON(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err)
	}
	return b
}
func Hash(v []byte) string { s := sha256.Sum256(v); return hex.EncodeToString(s[:]) }
func Now() time.Time       { return time.Now().UTC().Truncate(time.Microsecond) }

type Tool struct {
	ToolID      string    `json:"tool_id"`
	DisplayName string    `json:"display_name"`
	Description string    `json:"description"`
	Enabled     bool      `json:"enabled"`
	Revision    int64     `json:"revision"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
}
type ToolCreate struct {
	DisplayName string `json:"display_name"`
	Description string `json:"description"`
}
type ToolPatch struct {
	ExpectedRevision int64   `json:"expected_revision"`
	DisplayName      *string `json:"display_name,omitempty"`
	Description      *string `json:"description,omitempty"`
	Enabled          *bool   `json:"enabled,omitempty"`
}
type Revision struct {
	ExpectedRevision int64 `json:"expected_revision"`
}
type Binding struct {
	Knowledge          *KnowledgeBinding  `json:"knowledge,omitempty"`
	ImportID           string             `json:"import_id,omitempty"`
	OperationKey       string             `json:"operation_key,omitempty"`
	AuthScheme         string             `json:"auth_scheme,omitempty"`
	Effect             string             `json:"effect,omitempty"`
	ForwardIdempotency bool               `json:"forward_idempotency,omitempty"`
	HTTP               *apispec.Operation `json:"http,omitempty"` // compiler-owned; rejected on create input

	Prefix              string `json:"prefix,omitempty"`
	DelayMS             int    `json:"delay_ms,omitempty"`
	Fail                bool   `json:"fail,omitempty"`
	ConnectionVersionID string `json:"connection_version_id,omitempty"`
}
type VersionCreate struct {
	ExpectedRevision int64   `json:"expected_revision"`
	Kind             string  `json:"kind"`
	Binding          Binding `json:"binding"`
	TimeoutMS        int     `json:"timeout_ms"`
}
type ToolVersion struct {
	ToolVersionID    string          `json:"tool_version_id"`
	ToolID           string          `json:"tool_id"`
	Version          int             `json:"version"`
	Revision         int64           `json:"revision"`
	State            string          `json:"state"`
	Kind             string          `json:"kind"`
	InputSchema      json.RawMessage `json:"input_schema"`
	OutputSchema     json.RawMessage `json:"output_schema"`
	Binding          Binding         `json:"binding"`
	TimeoutMS        int             `json:"timeout_ms"`
	DefinitionSHA256 string          `json:"definition_sha256"`
	CreatedAt        time.Time       `json:"created_at"`
}
type ConnectionSpec struct {
	BaseURL            string `json:"base_url"`
	AuthType           string `json:"auth_type"`
	CredentialRef      string `json:"credential_ref,omitempty"`
	CredentialRevision string `json:"credential_revision,omitempty"`
	APIKeyIn           string `json:"api_key_in,omitempty"`
	APIKeyName         string `json:"api_key_name,omitempty"`
}
type ConnectionCreate struct {
	DisplayName string `json:"display_name"`
	ConnectionSpec
}
type ConnectionVersionCreate struct {
	ExpectedRevision int64 `json:"expected_revision"`
	ConnectionSpec
}
type Connection struct {
	ConnectionID     string    `json:"connection_id"`
	DisplayName      string    `json:"display_name"`
	Revision         int64     `json:"revision"`
	CreatedAt        time.Time `json:"created_at"`
	CurrentVersionID string    `json:"current_version_id"`
}
type ConnectionVersion struct {
	ConnectionVersionID string `json:"connection_version_id"`
	ConnectionID        string `json:"connection_id"`
	Version             int64  `json:"version"`
	ConnectionSpec
	CreatedAt time.Time `json:"created_at"`
}
type CallerContext struct {
	System      string `json:"system,omitempty"`
	WorkflowID  string `json:"workflow_id,omitempty"`
	ExecutionID string `json:"execution_id,omitempty"`
	NodeID      string `json:"node_id,omitempty"`
}
type CallCreate struct {
	ToolVersionID string          `json:"tool_version_id"`
	Arguments     json.RawMessage `json:"arguments"`
	Mode          string          `json:"mode"`
	CallerContext CallerContext   `json:"caller_context,omitempty"`
}
type Snapshot struct {
	ModelConnection *ConnectionVersion `json:"model_connection,omitempty"`
	Version         ToolVersion        `json:"version"`
	Connection      *ConnectionVersion `json:"connection,omitempty"`
	CallerContext   CallerContext      `json:"caller_context"`
}
type Call struct {
	ParentAgentRunID *string         `json:"parent_agent_run_id,omitempty"`
	ParentStepID     *string         `json:"parent_step_id,omitempty"`
	ToolCallID       string          `json:"tool_call_id"`
	ToolVersionID    string          `json:"tool_version_id"`
	RequestID        string          `json:"request_id"`
	InvocationKey    string          `json:"invocation_key"`
	InputSHA256      string          `json:"input_sha256"`
	DefinitionSHA256 string          `json:"definition_sha256"`
	Mode             string          `json:"mode"`
	State            string          `json:"state"`
	CallerContext    CallerContext   `json:"caller_context"`
	Result           json.RawMessage `json:"result,omitempty"`
	Error            *CallError      `json:"error,omitempty"`
	CreatedAt        time.Time       `json:"created_at"`
	StartedAt        *time.Time      `json:"started_at,omitempty"`
	CompletedAt      *time.Time      `json:"completed_at,omitempty"`
	DeadlineAt       time.Time       `json:"deadline_at"`
	QueueMS          int64           `json:"queue_ms"`
	ExecutionMS      int64           `json:"execution_ms"`
}
type CallError struct {
	Code    string `json:"code"`
	Message string `json:"message"`
	Stage   string `json:"stage"`
}
type Accepted struct {
	ToolCallID    string `json:"tool_call_id"`
	ToolVersionID string `json:"tool_version_id"`
	State         string `json:"state"`
	StatusURL     string `json:"status_url"`
}
type Page struct {
	Items      []json.RawMessage `json:"items"`
	NextCursor string            `json:"next_cursor,omitempty"`
}
type MCPEntry struct {
	Name        string      `json:"name"`
	DisplayName string      `json:"display_name"`
	Description string      `json:"description"`
	Available   bool        `json:"available"`
	Version     ToolVersion `json:"version"`
}

func ValidLabel(s string, max int, required bool) bool {
	return utf8.ValidString(s) && len(s) <= max && (!required || strings.TrimSpace(s) != "") && !strings.ContainsAny(s, "\x00\r\n")
}

var refPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`)

func ValidateConnection(s ConnectionSpec) error {
	u, err := url.Parse(s.BaseURL)
	if err != nil || len(s.BaseURL) > 2048 || u.Host == "" || u.User != nil || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" || (u.Scheme != "http" && u.Scheme != "https") || strings.ContainsAny(s.BaseURL, "\r\n\x00\\") {
		return E("TOOL_INPUT_INVALID")
	}
	if port := u.Port(); port != "" {
		n, e := strconv.Atoi(port)
		if e != nil || n < 1 || n > 65535 {
			return E("TOOL_INPUT_INVALID")
		}
	}
	if strings.Contains(u.Path, "%") || strings.Contains(u.RawPath, "%") {
		return E("TOOL_INPUT_INVALID")
	}
	for _, part := range strings.Split(u.Path, "/") {
		if part == "." || part == ".." {
			return E("TOOL_INPUT_INVALID")
		}
	}
	host := strings.TrimSuffix(strings.ToLower(u.Hostname()), ".")
	ip := net.ParseIP(host)
	if host == "100.100.100.200" || host == "168.63.129.16" || host == "fd00:ec2::254" {
		return E("TOOL_INPUT_INVALID")
	}
	if host == "localhost" || strings.HasSuffix(host, ".localhost") || (ip != nil && (ip.IsLoopback() || ip.IsUnspecified() || ip.IsLinkLocalUnicast() || ip.IsMulticast())) {
		return E("TOOL_INPUT_INVALID")
	}
	switch s.AuthType {
	case "none":
		if s.CredentialRef != "" || s.CredentialRevision != "" || s.APIKeyIn != "" || s.APIKeyName != "" {
			return E("TOOL_INPUT_INVALID")
		}
	case "bearer", "api_key":
		if !refPattern.MatchString(s.CredentialRef) || !refPattern.MatchString(s.CredentialRevision) {
			return E("TOOL_INPUT_INVALID")
		}
		if s.AuthType == "bearer" && (s.APIKeyIn != "" || s.APIKeyName != "") {
			return E("TOOL_INPUT_INVALID")
		}
		if s.AuthType == "api_key" && ((s.APIKeyIn != "header" && s.APIKeyIn != "query") || !refPattern.MatchString(s.APIKeyName)) {
			return E("TOOL_INPUT_INVALID")
		}
		if s.AuthType == "api_key" && s.APIKeyIn == "header" {
			switch strings.ToLower(s.APIKeyName) {
			case "host", "authorization", "cookie", "content-type", "content-length", "transfer-encoding", "connection", "accept", "accept-encoding", "upgrade", "te", "trailer", "expect", "forwarded", "idempotency-key":
				return E("TOOL_INPUT_INVALID")
			}
		}
	default:
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func Schemas(kind string) (json.RawMessage, json.RawMessage, error) {
	switch kind {
	case "knowledge_search_v1":
		return knowledgeSchemas()
	case "diagnostic_echo_v1":
		return json.RawMessage(`{"type":"object","properties":{"text":{"type":"string","maxLength":4096}},"required":["text"],"additionalProperties":false}`), json.RawMessage(`{"type":"object","properties":{"text":{"type":"string","maxLength":4352}},"required":["text"],"additionalProperties":false}`), nil
	case "diagnostic_connection_v1":
		return json.RawMessage(`{"type":"object","properties":{},"additionalProperties":false}`), json.RawMessage(`{"type":"object","properties":{"configured":{"type":"boolean","const":true},"connection_version_id":{"type":"string"},"credential_revision":{"type":"string"}},"required":["configured","connection_version_id","credential_revision"],"additionalProperties":false}`), nil
	default:
		return nil, nil, E("TOOL_INPUT_INVALID")
	}
}
func ValidateVersion(v VersionCreate) error {
	if v.Kind == "knowledge_search_v1" {
		return validateKnowledgeVersion(v)
	}
	if v.Binding.Knowledge != nil {
		return E("TOOL_INPUT_INVALID")
	}
	if v.Kind == "openapi_http_v1" {
		b := v.Binding
		if v.ExpectedRevision < 1 || v.TimeoutMS < 100 || v.TimeoutMS > 120000 || b.ConnectionVersionID == "" || b.ImportID == "" || b.OperationKey == "" || b.AuthScheme == "" || (b.Effect != "read" && b.Effect != "write") || b.HTTP != nil || b.Prefix != "" || b.DelayMS != 0 || b.Fail {
			return E("TOOL_INPUT_INVALID")
		}
		return nil
	}
	if v.Binding.ImportID != "" || v.Binding.OperationKey != "" || v.Binding.AuthScheme != "" || v.Binding.Effect != "" || v.Binding.ForwardIdempotency || v.Binding.HTTP != nil {
		return E("TOOL_INPUT_INVALID")
	}
	if v.ExpectedRevision < 1 || v.TimeoutMS < 100 || v.TimeoutMS > 30000 || v.Binding.DelayMS < 0 || v.Binding.DelayMS > 30000 || utf8.RuneCountInString(v.Binding.Prefix) > 256 {
		return E("TOOL_INPUT_INVALID")
	}
	switch v.Kind {
	case "diagnostic_echo_v1":
		if v.Binding.ConnectionVersionID != "" {
			return E("TOOL_INPUT_INVALID")
		}
	case "diagnostic_connection_v1":
		if v.Binding.Prefix != "" || v.Binding.DelayMS != 0 || v.Binding.Fail || v.Binding.ConnectionVersionID == "" {
			return E("TOOL_INPUT_INVALID")
		}
	default:
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func ValidateArguments(v ToolVersion, raw []byte) error {
	switch v.Kind {
	case "knowledge_search_v1":
		return validateKnowledgeArguments(v, raw)
	case "openapi_http_v1":
		args, err := apispec.JSON(raw, MaxInput)
		if err != nil {
			return E("TOOL_INPUT_INVALID")
		}
		schema, err := apispec.JSON(v.InputSchema, apispec.MaxOperation)
		if err != nil || apispec.Validate(schema, args) != nil {
			return E("TOOL_INPUT_INVALID")
		}
		return nil
	case "diagnostic_echo_v1":
		var in struct {
			Text *string `json:"text"`
		}
		if err := StrictJSON(raw, &in); err != nil {
			return err
		}
		if in.Text == nil || utf8.RuneCountInString(*in.Text) > 4096 {
			return E("TOOL_INPUT_INVALID")
		}
	case "diagnostic_connection_v1":
		var in map[string]json.RawMessage
		if err := StrictJSON(raw, &in); err != nil {
			return err
		}
		if in == nil || len(in) != 0 {
			return E("TOOL_INPUT_INVALID")
		}
	default:
		return E("TOOL_INPUT_INVALID")
	}
	return nil
}
func ValidateOutput(v ToolVersion, raw []byte) error {
	if len(raw) > MaxOutput {
		return E("TOOL_RESPONSE_INVALID")
	}
	switch v.Kind {
	case "diagnostic_echo_v1":
		var o struct {
			Text *string `json:"text"`
		}
		if StrictJSON(raw, &o) != nil || o.Text == nil || utf8.RuneCountInString(*o.Text) > 4352 {
			return E("TOOL_RESPONSE_INVALID")
		}
	case "diagnostic_connection_v1":
		var o struct {
			Configured          bool    `json:"configured"`
			ConnectionVersionID string  `json:"connection_version_id"`
			CredentialRevision  *string `json:"credential_revision"`
		}
		if StrictJSON(raw, &o) != nil || !o.Configured || o.ConnectionVersionID != v.Binding.ConnectionVersionID || o.CredentialRevision == nil {
			return E("TOOL_RESPONSE_INVALID")
		}
	default:
		return E("TOOL_RESPONSE_INVALID")
	}
	return nil
}
