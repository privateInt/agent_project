#!/bin/sh
set -eu
# Runs only when the Agent volume is first initialized. No shared DB is touched.
AGENT_APP_PASSWORD="$(cat /run/secrets/agent-db-app)"
export AGENT_APP_PASSWORD
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_password AGENT_APP_PASSWORD
CREATE ROLE agent_service LOGIN PASSWORD :'app_password'
 NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
REVOKE ALL ON DATABASE agent_platform FROM PUBLIC;
GRANT CONNECT ON DATABASE agent_platform TO agent_service;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA agent AUTHORIZATION agent_service;
ALTER ROLE agent_service IN DATABASE agent_platform SET search_path TO agent,pg_catalog;
SQL
unset AGENT_APP_PASSWORD
