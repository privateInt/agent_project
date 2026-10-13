// Package purge keeps permanent deletion serialized while retaining a durable tombstone.
package purge

import (
	"context"
	"github.com/jackc/pgx/v5/pgxpool"
	"time"
)

func Lock(ctx context.Context, pool *pgxpool.Pool, id string) (func(), error) {
	c, e := pool.Acquire(ctx)
	if e != nil {
		return nil, e
	}
	var locked bool
	e = c.QueryRow(ctx, `SELECT pg_advisory_lock(hashtextextended($1::text,0)) IS NULL`, "purge-files:"+id).Scan(&locked)
	if e != nil {
		_ = c.Conn().Close(context.Background())
		c.Release()
		return nil, e
	}
	return func() {
		cleanup, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		var released bool
		if e := c.QueryRow(cleanup, `SELECT pg_advisory_unlock(hashtextextended($1::text,0))`, "purge-files:"+id).Scan(&released); e != nil || !released {
			_ = c.Conn().Close(cleanup)
		}
		c.Release()
	}, nil
}
