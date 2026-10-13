package store

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/version"
	"context"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"errors"
	"fmt"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/protobuf/types/known/timestamppb"
	"net"
	"net/url"
	"strings"
	"time"
)

//go:embed migrations/*.sql
var migrations embed.FS
var ErrNotFound = errors.New("system check not found")

type Store struct {
	Pool     *pgxpool.Pool
	ToolLane string
}

func Connect(ctx context.Context) (*Store, error) {
	password, err := config.Secret("AGENT_DB_PASSWORD_FILE")
	if err != nil {
		return nil, err
	}
	u := url.URL{Scheme: "postgres", User: url.UserPassword("agent_service", password), Host: net.JoinHostPort(config.Env("AGENT_DB_HOST", "agent-postgres"), config.Env("AGENT_DB_PORT", "5432")), Path: "/agent_platform"}
	q := u.Query()
	q.Set("sslmode", config.Env("AGENT_DB_SSLMODE", "disable"))
	u.RawQuery = q.Encode()
	cfg, err := pgxpool.ParseConfig(u.String())
	if err != nil {
		return nil, errors.New("invalid Agent database configuration")
	}
	cfg.MaxConns = 4
	cfg.MinConns = 0
	cfg.ConnConfig.ConnectTimeout = 3 * time.Second
	cfg.ConnConfig.RuntimeParams["search_path"] = "agent,pg_catalog"
	cfg.ConnConfig.RuntimeParams["application_name"] = "agent-service"
	cfg.ConnConfig.RuntimeParams["statement_timeout"] = "5000"
	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, errors.New("Agent database connection failed")
	}
	s := &Store{Pool: pool}
	if err = s.VerifyIdentity(ctx); err != nil {
		pool.Close()
		return nil, err
	}
	if err = s.Migrate(ctx); err != nil {
		pool.Close()
		return nil, fmt.Errorf("Agent migration failed: %w", err)
	}
	return s, nil
}
func (s *Store) VerifyIdentity(ctx context.Context) error {
	var db, user string
	var elevated, member bool
	err := s.Pool.QueryRow(ctx, `SELECT current_database(),current_user,
 r.rolsuper OR r.rolcreatedb OR r.rolcreaterole OR r.rolreplication OR r.rolbypassrls,
 EXISTS(SELECT 1 FROM pg_auth_members WHERE member=r.oid)
 FROM pg_roles r WHERE r.rolname=current_user`).Scan(&db, &user, &elevated, &member)
	if err != nil {
		return errors.New("Agent database identity check failed")
	}
	if db != "agent_platform" || user != "agent_service" || elevated || member {
		return errors.New("Agent database requires its dedicated restricted role and database")
	}
	return nil
}
func (s *Store) Ping(ctx context.Context) error { return s.Pool.Ping(ctx) }
func (s *Store) Close()                         { s.Pool.Close() }
func (s *Store) Migrate(ctx context.Context) error {
	tx, err := s.Pool.Begin(ctx)
	if err != nil {
		return err
	}
	defer rollback(tx)
	if _, err = tx.Exec(ctx, `SELECT pg_advisory_xact_lock(16100,2)`); err != nil {
		return err
	}
	if _, err = tx.Exec(ctx, `CREATE TABLE IF NOT EXISTS agent.schema_migrations (
 version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`); err != nil {
		return err
	}
	rows, err := tx.Query(ctx, `SELECT version,checksum FROM agent.schema_migrations ORDER BY version`)
	if err != nil {
		return err
	}
	applied := map[int]string{}
	for rows.Next() {
		var v int
		var h string
		if err = rows.Scan(&v, &h); err != nil {
			rows.Close()
			return err
		}
		applied[v] = h
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	files, err := migrations.ReadDir("migrations")
	if err != nil {
		return err
	}
	if len(files) != version.Schema {
		return errors.New("migration manifest mismatch")
	}
	for v := range applied {
		if v < 1 || v > len(files) {
			return errors.New("database schema is newer than this service")
		}
	}
	for i, f := range files {
		v := i + 1
		if !strings.HasPrefix(f.Name(), fmt.Sprintf("%03d_", v)) {
			return errors.New("migration sequence mismatch")
		}
		data, err := migrations.ReadFile("migrations/" + f.Name())
		if err != nil {
			return err
		}
		h := sha256.Sum256(data)
		digest := hex.EncodeToString(h[:])
		if prior, ok := applied[v]; ok {
			if prior != digest {
				return fmt.Errorf("migration %d checksum mismatch", v)
			}
			continue
		}
		if _, err = tx.Exec(ctx, string(data)); err != nil {
			return err
		}
		if _, err = tx.Exec(ctx, `INSERT INTO agent.schema_migrations(version,checksum) VALUES($1,$2)`, v, digest); err != nil {
			return err
		}
	}
	return tx.Commit(ctx)
}
func (s *Store) Save(ctx context.Context, c *platformv1.SystemCheck) error {
	_, err := s.Pool.Exec(ctx, `INSERT INTO agent.system_checks(check_id,request_id,state,runtime_instance_id,error_code,created_at,completed_at) VALUES($1,$2,$3,$4,$5,$6,$7)`, c.CheckId, c.RequestId, c.State, c.RuntimeInstanceId, c.ErrorCode, c.CreatedAt.AsTime(), c.CompletedAt.AsTime())
	return err
}
func (s *Store) Get(ctx context.Context, id string) (*platformv1.SystemCheck, error) {
	c := &platformv1.SystemCheck{}
	var created, completed time.Time
	err := s.Pool.QueryRow(ctx, `SELECT check_id::text,request_id::text,state,runtime_instance_id,error_code,created_at,completed_at FROM agent.system_checks WHERE check_id=$1`, id).Scan(&c.CheckId, &c.RequestId, &c.State, &c.RuntimeInstanceId, &c.ErrorCode, &created, &completed)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, err
	}
	c.CreatedAt = timestamppb.New(created)
	c.CompletedAt = timestamppb.New(completed)
	return c, nil
}

// Cancellation must not cancel transaction cleanup. A bounded independent
// rollback releases locks before a connection is returned or discarded.
func rollback(tx pgx.Tx) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	_ = tx.Rollback(ctx)
}
