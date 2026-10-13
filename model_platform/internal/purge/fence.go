package purge

import (
	"context"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const Schema = `CREATE TABLE IF NOT EXISTS model_purge_fences(model_version_id UUID PRIMARY KEY);`

func ValidID(id string) error {
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id {
		return status.Error(codes.InvalidArgument, "canonical model UUID required")
	}
	return nil
}
func VersionLock(ctx context.Context, tx pgx.Tx, id string) error {
	if e := ValidID(id); e != nil {
		return e
	}
	_, e := tx.Exec(ctx, `SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))`, "model-purge:"+id)
	return e
}
func RequireAvailable(ctx context.Context, tx pgx.Tx, id string) error {
	if e := VersionLock(ctx, tx, id); e != nil {
		return e
	}
	var blocked bool
	if e := tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM model_purge_fences WHERE model_version_id=$1)`, id).Scan(&blocked); e != nil {
		return e
	}
	if blocked {
		return status.Error(codes.FailedPrecondition, "model files are permanently deleted or deletion is pending")
	}
	return nil
}
func Fence(ctx context.Context, tx pgx.Tx, id string) error {
	_, e := tx.Exec(ctx, `INSERT INTO model_purge_fences(model_version_id) VALUES($1) ON CONFLICT DO NOTHING`, id)
	return e
}
