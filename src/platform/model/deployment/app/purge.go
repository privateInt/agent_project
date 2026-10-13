package app

import (
	"context"
    "errors"
    "github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	deploymentv1 "model-platform/gen/deployment/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
)

func (s *Service) BlockModelPurge(ctx context.Context, r *deploymentv1.ModelPurgeFence) (*deploymentv1.ModelPurgeFence, error) {
	if e := purge.ValidID(r.GetModelVersionId()); e != nil {
		return nil, e
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer tx.Rollback(context.Background())
	if e = purge.VersionLock(ctx, tx, r.ModelVersionId); e != nil {
		return nil, e
	}
	rows, e := tx.Query(ctx, `SELECT deployment_id::text,engine,status,desired_state,runtime_state FROM deployments WHERE model_version_id=$1 AND deleted_at IS NULL ORDER BY deployment_id`, r.ModelVersionId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	var caches []string
	active := false
	for rows.Next() {
		var id, engine, state, desired, runtime string
		if e = rows.Scan(&id, &engine, &state, &desired, &runtime); e != nil {
			rows.Close()
			return nil, e
		}
		if engine == "demo" {
			if state != "failed" {
				active = true
			}
		} else {
			if state != "stopped" || desired != "stopped" || runtime != "stopped" {
				active = true
			}
			caches = append(caches, id)
		}
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	if active {
		return nil, status.Error(codes.FailedPrecondition, "MODEL_DEPLOYED: stop every deployment of this model first")
	}
	if e = purge.Fence(ctx, tx, r.ModelVersionId); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	for _, id := range caches {
		if s.runtime == nil {
			return nil, status.Error(codes.Unavailable, "cache purge controller unavailable")
		}
		remover, ok := s.runtime.fetcher.(interface {
			Purge(context.Context, string) error
		})
		if !ok {
			return nil, status.Error(codes.Unavailable, "cache purge unavailable")
		}
		// Stop acknowledgment is produced under this same serving session lock.
		c, e := s.pool.Acquire(ctx)
		if e != nil {
			return nil, e
		}
		_, e = c.Exec(ctx, `SELECT pg_advisory_lock(hashtextextended($1::text,0))`, "serving:"+id)
		if e == nil {
			plan, loadErr := loadServing(ctx, c, id)
			if errors.Is(loadErr, pgx.ErrNoRows) {
                var deleted bool
                e = c.QueryRow(ctx, `SELECT deleted_at IS NOT NULL FROM deployments WHERE deployment_id=$1`, id).Scan(&deleted)
                if e == nil && !deleted { e = loadErr }
            } else if loadErr != nil {
				e = loadErr
			} else if plan.desired != "stopped" || plan.state != "stopped" {
				e = status.Error(codes.FailedPrecondition, "deployment stop is unconfirmed")
			} else {
				evidence, inspectErr := s.runtime.driver.Inspect(ctx, plan.plan)
				if inspectErr != nil {
					e = inspectErr
				} else if evidence != nil && (evidence.State.Running || evidence.State.Paused || evidence.State.Restarting || evidence.State.Status != "exited") {
					e = status.Error(codes.FailedPrecondition, "deployment container is still active")
				} else {
					e = remover.Purge(ctx, id)
				}
			}
		}
		// Closing a dedicated connection always releases the serving lock.
		_ = c.Conn().Close(context.Background())
		c.Release()
		if e != nil {
			return nil, status.Error(codes.FailedPrecondition, "deployment cache cleanup unconfirmed; retry after stopping its runtime")
		}
	}
	return &deploymentv1.ModelPurgeFence{ModelVersionId: r.ModelVersionId}, nil
}
