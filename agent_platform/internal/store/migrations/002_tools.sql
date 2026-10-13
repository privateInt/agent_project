CREATE TABLE agent.tools (
 tool_id UUID PRIMARY KEY, revision BIGINT NOT NULL CHECK(revision>0), document JSONB NOT NULL
);
CREATE TABLE agent.tool_versions (
 tool_version_id UUID PRIMARY KEY, tool_id UUID NOT NULL REFERENCES agent.tools(tool_id),
 version INTEGER NOT NULL CHECK(version>0), revision BIGINT NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('draft','published','retired')), document JSONB NOT NULL,
 UNIQUE(tool_id,version)
);
CREATE TABLE agent.connections (
 connection_id UUID PRIMARY KEY, revision BIGINT NOT NULL CHECK(revision>0), document JSONB NOT NULL
);
CREATE TABLE agent.connection_versions (
 connection_version_id UUID PRIMARY KEY, connection_id UUID NOT NULL REFERENCES agent.connections(connection_id),
 version BIGINT NOT NULL CHECK(version>0), document JSONB NOT NULL, UNIQUE(connection_id,version)
);
CREATE TABLE agent.tool_calls (
 tool_call_id UUID PRIMARY KEY, tool_version_id UUID NOT NULL REFERENCES agent.tool_versions(tool_version_id),
 request_id UUID NOT NULL, principal TEXT NOT NULL, invocation_key UUID NOT NULL,
 fingerprint TEXT NOT NULL, input_sha256 TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('run','test')),
 execution_lane TEXT NOT NULL DEFAULT 'live',
 state TEXT NOT NULL CHECK(state IN ('accepted','running','succeeded','failed','interrupted')),
 arguments JSONB NOT NULL, snapshot JSONB NOT NULL, result JSONB, error_code TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
 deadline_at TIMESTAMPTZ NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ,
 UNIQUE(principal,invocation_key),
 CHECK((state IN ('accepted','running') AND completed_at IS NULL) OR (state IN ('succeeded','failed','interrupted') AND completed_at IS NOT NULL)),
 CHECK(state <> 'running' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
 CHECK(state <> 'succeeded' OR (result IS NOT NULL AND error_code=''))
);
CREATE INDEX tool_calls_pending_idx ON agent.tool_calls(created_at,tool_call_id) WHERE state='accepted';
CREATE INDEX tool_calls_running_idx ON agent.tool_calls(lease_until) WHERE state='running';
CREATE TABLE agent.tool_idempotency (
 principal TEXT NOT NULL, operation TEXT NOT NULL, resource_id TEXT NOT NULL, key UUID NOT NULL,
 fingerprint TEXT NOT NULL, response JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
 PRIMARY KEY(principal,operation,resource_id,key)
);
COMMENT ON TABLE agent.tool_calls IS 'Agent-owned durable Tool calls; accepted jobs may resume, running jobs are never automatically replayed';
