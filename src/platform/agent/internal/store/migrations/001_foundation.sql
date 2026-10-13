CREATE TABLE agent.system_checks (
 check_id UUID PRIMARY KEY,
 request_id UUID NOT NULL,
 state TEXT NOT NULL CHECK (state IN ('succeeded', 'failed')),
 runtime_instance_id TEXT NOT NULL DEFAULT '',
 error_code TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL,
 completed_at TIMESTAMPTZ NOT NULL,
 CHECK (completed_at >= created_at),
 CHECK ((state = 'succeeded' AND runtime_instance_id <> '' AND error_code = '')
     OR (state = 'failed' AND error_code <> ''))
);
CREATE INDEX system_checks_created_idx ON agent.system_checks (created_at DESC, check_id DESC);
COMMENT ON TABLE agent.system_checks IS 'A02 diagnostics only; never AgentRun or ToolCall records';
