CREATE TABLE agent.agents (
 agent_id UUID PRIMARY KEY, revision BIGINT NOT NULL CHECK(revision>0), document JSONB NOT NULL
);
CREATE TABLE agent.agent_versions (
 agent_version_id UUID PRIMARY KEY, agent_id UUID NOT NULL REFERENCES agent.agents(agent_id),
 version INTEGER NOT NULL CHECK(version>0), revision BIGINT NOT NULL CHECK(revision>0),
 state TEXT NOT NULL CHECK(state IN ('draft','published','retired')),
 tool_version_id UUID NOT NULL REFERENCES agent.tool_versions(tool_version_id),
 document JSONB NOT NULL, UNIQUE(agent_id,version)
);
CREATE TABLE agent.agent_runs (
 agent_run_id UUID PRIMARY KEY, agent_version_id UUID NOT NULL REFERENCES agent.agent_versions(agent_version_id),
 request_id UUID NOT NULL, principal TEXT NOT NULL, invocation_key UUID NOT NULL,
 fingerprint TEXT NOT NULL, input_sha256 TEXT NOT NULL, input JSONB NOT NULL, snapshot JSONB NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('test','run')), execution_lane TEXT NOT NULL DEFAULT 'live',
 state TEXT NOT NULL CHECK(state IN ('accepted','running','succeeded','failed','interrupted')),
 result JSONB, error_code TEXT NOT NULL DEFAULT '', error_stage TEXT NOT NULL DEFAULT '',
 tool_call_id UUID REFERENCES agent.tool_calls(tool_call_id),
 created_at TIMESTAMPTZ NOT NULL, started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
 deadline_at TIMESTAMPTZ NOT NULL, lease_token UUID, lease_until TIMESTAMPTZ,
 UNIQUE(principal,invocation_key),
 CHECK((state IN ('accepted','running') AND completed_at IS NULL) OR (state IN ('succeeded','failed','interrupted') AND completed_at IS NOT NULL)),
 CHECK(state <> 'running' OR (lease_token IS NOT NULL AND lease_until IS NOT NULL)),
 CHECK(state <> 'succeeded' OR (result IS NOT NULL AND error_code=''))
);
CREATE INDEX agent_runs_pending_idx ON agent.agent_runs(created_at,agent_run_id) WHERE state='accepted';
CREATE INDEX agent_runs_running_idx ON agent.agent_runs(lease_until) WHERE state='running';
CREATE TABLE agent.agent_steps (
 agent_run_id UUID NOT NULL REFERENCES agent.agent_runs(agent_run_id),
 step_id TEXT NOT NULL CHECK(step_id IN ('validate','search','context','generate','finalize')),
 position INTEGER NOT NULL, attempt INTEGER NOT NULL DEFAULT 1 CHECK(attempt=1),
 state TEXT NOT NULL CHECK(state IN ('pending','running','succeeded','failed','interrupted','skipped')),
 started_at TIMESTAMPTZ, completed_at TIMESTAMPTZ, detail JSONB NOT NULL DEFAULT '{}'::jsonb,
 PRIMARY KEY(agent_run_id,step_id), UNIQUE(agent_run_id,position)
);
ALTER TABLE agent.tool_calls ADD COLUMN parent_agent_run_id UUID REFERENCES agent.agent_runs(agent_run_id);
ALTER TABLE agent.tool_calls ADD COLUMN parent_step_id TEXT;
ALTER TABLE agent.tool_calls ADD CONSTRAINT tool_agent_parent_check CHECK((parent_agent_run_id IS NULL AND parent_step_id IS NULL) OR (parent_agent_run_id IS NOT NULL AND parent_step_id='search'));
CREATE UNIQUE INDEX tool_agent_parent_idx ON agent.tool_calls(parent_agent_run_id,parent_step_id) WHERE parent_agent_run_id IS NOT NULL;
COMMENT ON TABLE agent.agent_runs IS 'Agent-owned durable queue and snapshots. Expired running leases are interrupted, never replayed automatically.';
