package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/apispec"
	"agent-platform/internal/rpc"
	"agent-platform/internal/tooling"
	"context"
	"encoding/json"
	"errors"
	"github.com/jackc/pgx/v5"
	"strings"
	"time"
)

type OpenAPIImport struct {
	ImportID       string            `json:"import_id"`
	DocumentSHA256 string            `json:"document_sha256"`
	Format         string            `json:"format"`
	CreatedAt      time.Time         `json:"created_at"`
	Analysis       *apispec.Analysis `json:"analysis"`
}

func (s *Store) createImport(ctx context.Context, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	if len(in.Json) > apispec.MaxDocument {
		return nil, tooling.E("TOOL_PAYLOAD_TOO_LARGE")
	}
	digest := tooling.Hash(in.Json)
	fingerprint := tooling.Hash(append([]byte(in.DocumentFormat+":"), in.Json...))
	analysis, e := apispec.Parse(in.Json, in.DocumentFormat)
	if e != nil {
		return nil, tooling.E("OPENAPI_INVALID")
	}
	tx, e := s.Pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer rollback(tx)
	if _, e = tx.Exec(ctx, "SELECT pg_advisory_xact_lock(16100,3)"); e != nil {
		return nil, e
	}
	var oldHash string
	var response []byte
	e = tx.QueryRow(ctx, `SELECT fingerprint,response FROM agent.tool_idempotency WHERE principal=$1 AND operation='CreateOpenAPIImport' AND resource_id='' AND key=$2`, tooling.Principal, in.IdempotencyKey).Scan(&oldHash, &response)
	if e == nil {
		if oldHash != fingerprint {
			return nil, tooling.E("KEY_CONFLICT")
		}
		return &platformv1.ToolResponse{Json: response, Replayed: true}, nil
	}
	if !errors.Is(e, pgx.ErrNoRows) {
		return nil, e
	}
	e = tx.QueryRow(ctx, `SELECT document FROM agent.openapi_imports WHERE document_sha256=$1 AND document_format=$2`, digest, in.DocumentFormat).Scan(&response)
	if errors.Is(e, pgx.ErrNoRows) {
		var count int
		if e = tx.QueryRow(ctx, `SELECT count(*) FROM agent.openapi_imports`).Scan(&count); e != nil {
			return nil, e
		}
		if count >= 100 {
			return nil, tooling.E("TOOL_LIMIT_REACHED")
		}
		imp := OpenAPIImport{ImportID: rpc.NewID(), DocumentSHA256: digest, Format: in.DocumentFormat, CreatedAt: tooling.Now(), Analysis: analysis}
		response = tooling.JSON(imp)
		_, e = tx.Exec(ctx, `INSERT INTO agent.openapi_imports(import_id,document_sha256,document_format,source,document) VALUES($1,$2,$3,$4,$5)`, imp.ImportID, digest, in.DocumentFormat, in.Json, response)
	}
	if e != nil {
		return nil, e
	}
	if _, e = tx.Exec(ctx, `INSERT INTO agent.tool_idempotency(principal,operation,resource_id,key,fingerprint,response) VALUES($1,'CreateOpenAPIImport','',$2,$3,$4)`, tooling.Principal, in.IdempotencyKey, fingerprint, response); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, e
	}
	return &platformv1.ToolResponse{Json: response}, nil
}
func (s *Store) readImport(ctx context.Context, op string, in *platformv1.ToolRequest) (*platformv1.ToolResponse, error) {
	if op == "GetOpenAPIImport" {
		if !rpc.ValidID(in.ResourceId) {
			return nil, tooling.E("TOOL_INPUT_INVALID")
		}
		var raw []byte
		e := s.Pool.QueryRow(ctx, `SELECT document FROM agent.openapi_imports WHERE import_id=$1`, in.ResourceId).Scan(&raw)
		if errors.Is(e, pgx.ErrNoRows) {
			return nil, tooling.E("OPENAPI_NOT_FOUND")
		}
		return &platformv1.ToolResponse{Json: raw}, e
	}
	limit := int(in.Limit)
	if limit == 0 {
		limit = 20
	}
	if limit < 1 || limit > 100 || in.Cursor != "" && !rpc.ValidID(in.Cursor) {
		return nil, tooling.E("TOOL_INPUT_INVALID")
	}
	cursor := in.Cursor
	if cursor == "" {
		cursor = "00000000-0000-0000-0000-000000000000"
	}
	rows, e := s.Pool.Query(ctx, `SELECT import_id::text,document-'analysis' || jsonb_build_object('title',document->'analysis'->'title','spec_version',document->'analysis'->'spec_version') FROM agent.openapi_imports WHERE import_id>$1 ORDER BY import_id LIMIT $2`, cursor, limit+1)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	page := tooling.Page{Items: []json.RawMessage{}}
	last := ""
	for rows.Next() {
		var id string
		var raw []byte
		if e = rows.Scan(&id, &raw); e != nil {
			return nil, e
		}
		if len(page.Items) == limit {
			page.NextCursor = last
			break
		}
		page.Items = append(page.Items, raw)
		last = id
	}
	if e = rows.Err(); e != nil {
		return nil, e
	}
	return &platformv1.ToolResponse{Json: tooling.JSON(page)}, nil
}
func (s *Store) compileHTTP(ctx context.Context, tx pgx.Tx, in *tooling.VersionCreate) (json.RawMessage, json.RawMessage, error) {
	invalid := func(code string) (json.RawMessage, json.RawMessage, error) { return nil, nil, tooling.E(code) }
	b := &in.Binding
	if !rpc.ValidID(b.ImportID) || !rpc.ValidID(b.ConnectionVersionID) {
		return invalid("TOOL_INPUT_INVALID")
	}
	var imp OpenAPIImport
	if e := loadDocument(ctx, tx, "openapi_imports", "import_id", b.ImportID, &imp); e != nil {
		return nil, nil, e
	}
	var operation *apispec.Operation
	for _, candidate := range imp.Analysis.Operations {
		if candidate.Key == b.OperationKey {
			c := candidate
			operation = &c
			break
		}
	}
	if operation == nil || !operation.Supported {
		return invalid("OPENAPI_UNSUPPORTED")
	}
	var connection tooling.ConnectionVersion
	if e := loadDocument(ctx, tx, "connection_versions", "connection_version_id", b.ConnectionVersionID, &connection); e != nil {
		return nil, nil, e
	}
	if tooling.ValidateConnection(connection.ConnectionSpec) != nil {
		return invalid("CONNECTION_UNAVAILABLE")
	}
	valid := false
	for _, auth := range operation.AuthOptions {
		if auth.Name != b.AuthScheme {
			continue
		}
		if auth.Type == connection.AuthType && (auth.Type != "api_key" || (auth.In == connection.APIKeyIn && strings.EqualFold(auth.Key, connection.APIKeyName))) {
			// Query keys are case-sensitive; header names are case-insensitive.
			if auth.Type == "api_key" && auth.In == "query" && auth.Key != connection.APIKeyName {
				continue
			}
			valid = true
		}
	}
	if !valid {
		return invalid("CONNECTION_UNAVAILABLE")
	}
	for _, p := range operation.Parameters {
		if connection.AuthType == "api_key" && p.In == connection.APIKeyIn && ((p.In == "header" && strings.EqualFold(p.Name, connection.APIKeyName)) || p.Name == connection.APIKeyName) {
			return invalid("OPENAPI_UNSUPPORTED")
		}
	}
	b.HTTP = operation
	// HTTP output schemas remain status-specific in the immutable binding.
	output := tooling.JSON(map[string]any{"type": "object", "properties": map[string]any{"status": map[string]any{"type": "integer"}, "completion": map[string]any{"type": "string"}, "data": map[string]any{}}, "required": []string{"status", "completion", "data"}, "additionalProperties": false})
	return tooling.JSON(operation.InputSchema), output, nil
}
