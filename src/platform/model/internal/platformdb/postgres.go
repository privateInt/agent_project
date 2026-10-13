package platformdb

import (
	"context"
	"errors"
	"fmt"
	"log"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
)

const connectRetryInterval = time.Second

// Connect is the common PostgreSQL bootstrap entrypoint used by every domain service.
//
// POSTGRES_URL is a single bootstrap connection shared by all domain services.
// It normally points at the built-in "postgres" maintenance database. Connect rewrites
// the database name to expectedDatabase internally, creates that database when missing,
// and then opens the application pool. This removes both manual CREATE DATABASE commands
// and four duplicated per-service database URLs from the E2E setup.
//
// The PostgreSQL role in POSTGRES_URL needs CONNECT plus CREATEDB privilege for the first
// bootstrap. Once the databases exist, normal reconnects only need the permissions
// required by each application's schema/data operations.
func Connect(ctx context.Context, postgresURL, expectedDatabase string) (*pgxpool.Pool, error) {
	cfg, err := pgxpool.ParseConfig(postgresURL)
	if err != nil {
		return nil, fmt.Errorf("parse postgres config: %w", err)
	}
	// The shared URL can point at postgres (recommended) or another existing DB.
	// The actual application connection is always rewritten to its expected DB.
	cfg.ConnConfig.Database = expectedDatabase

	if err := ensureDatabaseWithRetry(ctx, cfg.ConnConfig, expectedDatabase); err != nil {
		return nil, err
	}

	pool, err := connectPoolWithRetry(ctx, cfg)
	if err != nil {
		return nil, err
	}
	log.Printf("postgres database ready: %s", expectedDatabase)
	return pool, nil
}

func ensureDatabaseWithRetry(ctx context.Context, target *pgx.ConnConfig, database string) error {
	for {
		err := ensureDatabaseOnce(ctx, target, database)
		if err == nil {
			return nil
		}
		if ctx.Err() != nil {
			return fmt.Errorf("bootstrap postgres database %q: %w", database, err)
		}
		log.Printf("postgres bootstrap waiting database=%s error=%v", database, err)
		if err := sleepContext(ctx, connectRetryInterval); err != nil {
			return fmt.Errorf("bootstrap postgres database %q: %w", database, err)
		}
	}
}

func ensureDatabaseOnce(ctx context.Context, target *pgx.ConnConfig, database string) error {
	maintenance := target.Copy()
	maintenance.Database = "postgres"

	conn, err := pgx.ConnectConfig(ctx, maintenance)
	if err != nil {
		return fmt.Errorf("connect postgres maintenance database: %w", err)
	}
	defer conn.Close(context.Background())

	var exists bool
	if err := conn.QueryRow(
		ctx,
		`SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname=$1)`,
		database,
	).Scan(&exists); err != nil {
		return fmt.Errorf("check postgres database %q: %w", database, err)
	}
	if exists {
		return nil
	}

	_, err = conn.Exec(ctx, "CREATE DATABASE "+pgx.Identifier{database}.Sanitize())
	if err != nil {
		var pgErr *pgconn.PgError
		// Concurrent service startup can race after both observe "not exists".
		// duplicate_database means another process won the race, which is success.
		if errors.As(err, &pgErr) && pgErr.Code == "42P04" {
			return nil
		}
		return fmt.Errorf("create postgres database %q: %w", database, err)
	}

	log.Printf("created postgres database: %s", database)
	return nil
}

func connectPoolWithRetry(ctx context.Context, cfg *pgxpool.Config) (*pgxpool.Pool, error) {
	for {
		pool, err := pgxpool.NewWithConfig(ctx, cfg)
		if err == nil {
			err = pool.Ping(ctx)
			if err == nil {
				return pool, nil
			}
			pool.Close()
		}

		if ctx.Err() != nil {
			if err == nil {
				err = ctx.Err()
			}
			return nil, fmt.Errorf("connect postgres database %q: %w", cfg.ConnConfig.Database, err)
		}

		log.Printf("postgres connection waiting database=%s error=%v", cfg.ConnConfig.Database, err)
		if sleepErr := sleepContext(ctx, connectRetryInterval); sleepErr != nil {
			return nil, fmt.Errorf("connect postgres database %q: %w", cfg.ConnConfig.Database, sleepErr)
		}
	}
}

func sleepContext(ctx context.Context, d time.Duration) error {
	timer := time.NewTimer(d)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}
