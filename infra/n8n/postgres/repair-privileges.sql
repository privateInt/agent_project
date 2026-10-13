-- Existing volumes skip init-flows.sh. Run this narrow, idempotent repair as flows_admin.
-- No password, key, table, schema ownership or cluster-wide role attribute changes.
BEGIN;
DO $$
BEGIN
  IF current_database() <> 'flows' OR current_user <> 'flows_admin' THEN
    RAISE EXCEPTION 'Expected flows_admin connected to flows';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'flows_service') THEN
    RAISE EXCEPTION 'Expected existing flows_service role';
  END IF;
  IF NOT EXISTS (
    SELECT 1 FROM pg_namespace n JOIN pg_roles r ON r.oid = n.nspowner
    WHERE n.nspname = 'n8n' AND r.rolname = 'flows_service'
  ) THEN
    RAISE EXCEPTION 'Expected existing n8n schema owned by flows_service';
  END IF;
END
$$;
GRANT CONNECT, CREATE ON DATABASE flows TO flows_service;
COMMIT;
