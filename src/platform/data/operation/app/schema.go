package app

import (
	"context"

	"data-platform/operation/outbox"

	"github.com/jackc/pgx/v5/pgxpool"
)

func InitSchema(ctx context.Context, pool *pgxpool.Pool) error {
	_, err := pool.Exec(ctx, `
CREATE TABLE IF NOT EXISTS operations (
    operation_id UUID PRIMARY KEY,
    kind VARCHAR(128) NOT NULL,
    execution_type VARCHAR(128) NOT NULL,
    resource_type VARCHAR(128) NOT NULL,
    resource_id UUID NOT NULL,
    status VARCHAR(32) NOT NULL,
    phase VARCHAR(64) NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    error_code VARCHAR(128),
    error_message TEXT,
    retryable BOOLEAN NOT NULL DEFAULT FALSE,
    resources JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    CONSTRAINT operation_status_check CHECK (status IN ('pending','dispatching','running','retry_wait','succeeded','failed','cancelled'))
);
CREATE INDEX IF NOT EXISTS operations_browse_idx ON operations(created_at DESC,operation_id DESC);
CREATE INDEX IF NOT EXISTS operations_status_idx ON operations(status, created_at);
CREATE INDEX IF NOT EXISTS operations_resource_idx ON operations(resource_type, resource_id, created_at DESC);
ALTER TABLE operations ADD COLUMN IF NOT EXISTS progress_json JSONB;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS progress_updated_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS creation_request_hash VARCHAR(64);
ALTER TABLE operations ADD COLUMN IF NOT EXISTS dispatch_protocol INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS dispatch_generation INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS dispatch_token UUID;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS dispatch_owner TEXT NOT NULL DEFAULT '';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS dispatch_lease_until TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS execution_started_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS execution_instance_id UUID;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS reconcile_checked_at TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS reconcile_note TEXT NOT NULL DEFAULT '';
ALTER TABLE operations ADD COLUMN IF NOT EXISTS gpu_wait_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS gpu_wait_until TIMESTAMPTZ;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS gpu_wait_token UUID;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS resources JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE operations DROP CONSTRAINT IF EXISTS operation_status_check;
ALTER TABLE operations ADD CONSTRAINT operation_status_check CHECK(status IN ('pending','dispatching','running','retry_wait','cancelling','succeeded','failed','cancelled'));
`)
	if err != nil {
		return err
	}
	return outbox.InitSchema(ctx, pool)
}
