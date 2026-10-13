#!/bin/sh
set -eu
PGPASSWORD="$(cat /run/secrets/flows-db-app)"
export PGPASSWORD
psql -X --set ON_ERROR_STOP=1 --host 127.0.0.1 --username flows_service --dbname flows <<'SQL'
SELECT current_database(), current_user, current_schema();
BEGIN;
-- Exercise the exact startup statement; table DDL alone misses DB CREATE.
CREATE SCHEMA IF NOT EXISTS "n8n";
CREATE TABLE n8n.__infra_startup_probe (id uuid DEFAULT n8n.uuid_generate_v4());
INSERT INTO n8n.__infra_startup_probe DEFAULT VALUES;
ROLLBACK;
SQL
unset PGPASSWORD
