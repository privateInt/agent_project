#!/bin/sh
set -eu
FLOWS_APP_PASSWORD="$(cat /run/secrets/flows-db-app)"
export FLOWS_APP_PASSWORD
psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv app_password FLOWS_APP_PASSWORD
CREATE ROLE flows_service LOGIN PASSWORD :'app_password'
 NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS NOINHERIT;
REVOKE ALL ON DATABASE flows FROM PUBLIC;
-- n8n TypeORM issues CREATE SCHEMA IF NOT EXISTS even when n8n already exists.
-- Database-scoped CREATE is required; role-level CREATEDB stays disabled.
GRANT CONNECT, CREATE ON DATABASE flows TO flows_service;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA n8n AUTHORIZATION flows_service;
-- n8n TypeORM uses uuid_generate_v4; install once as the DB owner.
CREATE EXTENSION IF NOT EXISTS "uuid-ossp" WITH SCHEMA n8n;
ALTER ROLE flows_service IN DATABASE flows SET search_path TO n8n,pg_catalog;
SQL
unset FLOWS_APP_PASSWORD
