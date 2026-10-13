CREATE TABLE agent.openapi_imports (
 import_id UUID PRIMARY KEY,
 document_sha256 TEXT NOT NULL,
 document_format TEXT NOT NULL CHECK(document_format IN ('json','yaml')),
 source BYTEA NOT NULL,
 document JSONB NOT NULL,
 UNIQUE(document_sha256,document_format)
);
ALTER TABLE agent.tool_calls DROP CONSTRAINT tool_calls_state_check;
ALTER TABLE agent.tool_calls DROP CONSTRAINT tool_calls_check;
ALTER TABLE agent.tool_calls ADD CONSTRAINT tool_calls_state_check CHECK(state IN ('accepted','running','succeeded','failed','interrupted','external_pending','unknown'));
ALTER TABLE agent.tool_calls ADD CONSTRAINT tool_calls_completion_check CHECK((state IN ('accepted','running') AND completed_at IS NULL) OR (state IN ('succeeded','failed','interrupted','external_pending','unknown') AND completed_at IS NOT NULL));
COMMENT ON TABLE agent.openapi_imports IS 'Agent-owned immutable OpenAPI source and compiler analysis; never fetches remote references';
