CREATE TABLE agent.workflow_links (
 workflow_link_id uuid PRIMARY KEY,
 agent_version_id uuid NOT NULL REFERENCES agent.agent_versions(agent_version_id),
 instance_url text NOT NULL,
 workflow_id text NOT NULL,
 revision bigint NOT NULL,
 document jsonb NOT NULL,
 UNIQUE(instance_url,workflow_id)
);
