// Package idempotency commits a domain mutation and its replay response in
// one transaction. No API key or request body is persisted in this table.
package idempotency

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"regexp"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const Schema = `
CREATE TABLE IF NOT EXISTS api_requests (
 scope TEXT NOT NULL,
 key_hash VARCHAR(64) NOT NULL,
 request_hash VARCHAR(64) NOT NULL,
 response BYTEA,
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 PRIMARY KEY(scope,key_hash)
);
`

var keyPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$`)

func ValidateKey(key string) error {
	if key != "" && !keyPattern.MatchString(key) {
		return status.Error(codes.InvalidArgument, "Idempotency-Key must start with a letter or digit and contain 1..128 ASCII letters, digits, '.', '_', ':' or '-'")
	}
	return nil
}

// Fingerprint expects an already normalized, explicit versioned request shape.
// Credential-bearing requests use HMAC with the domain's encryption key.
func Fingerprint(value any, secret []byte) (string, error) {
	b, err := json.Marshal(value)
	if err != nil {
		return "", status.Error(codes.InvalidArgument, "request cannot be normalized")
	}
	if len(secret) > 0 {
		h := hmac.New(sha256.New, secret)
		h.Write(b)
		return hex.EncodeToString(h.Sum(nil)), nil
	}
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:]), nil
}

func DBError(err error) error {
	if err == nil {
		return nil
	}
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(err, context.Canceled) {
		return status.FromContextError(err).Err()
	}
	var pgerr *pgconn.PgError
	if errors.As(err, &pgerr) && pgerr.Code == "23505" {
		return status.Error(codes.AlreadyExists, "resource already exists")
	}
	if _, ok := status.FromError(err); ok {
		return err
	}
	// SQL error detail can contain user input. Keep it out of public replies.
	return status.Error(codes.Internal, "request persistence failed; retry with the same Idempotency-Key")
}

func Rollback(tx pgx.Tx) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	_ = tx.Rollback(ctx)
}

// Execute never exposes an in-progress key reservation: the mutation, optional
// submission, and encoded response all commit together. Conflicting INSERTs
// serialize at the primary key. A subsequent SELECT sees the committed winner.
func Execute(ctx context.Context, pool *pgxpool.Pool, scope, key, fingerprint string, response proto.Message, create func(context.Context, pgx.Tx) error) error {
	return ExecuteWithTimeout(ctx, pool, scope, key, fingerprint, response, 15*time.Second, create)
}

// ExecuteWithTimeout is used for bounded synchronous blob writes. Existing
// request-only mutations retain their original 15 second transaction budget.
func ExecuteWithTimeout(ctx context.Context, pool *pgxpool.Pool, scope, key, fingerprint string, response proto.Message, timeout time.Duration, create func(context.Context, pgx.Tx) error) error {
	if timeout <= 0 || timeout > 120*time.Second {
		return status.Error(codes.InvalidArgument, "invalid transaction timeout")
	}
	if err := ValidateKey(key); err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	tx, err := pool.BeginTx(ctx, pgx.TxOptions{IsoLevel: pgx.ReadCommitted})
	if err != nil {
		return DBError(err)
	}
	defer Rollback(tx)
	keyHash := ""
	if key != "" {
		h := sha256.Sum256([]byte(key))
		keyHash = hex.EncodeToString(h[:])
		tag, err := tx.Exec(ctx, `INSERT INTO api_requests(scope,key_hash,request_hash) VALUES($1,$2,$3) ON CONFLICT(scope,key_hash) DO NOTHING`, scope, keyHash, fingerprint)
		if err != nil {
			return DBError(err)
		}
		if tag.RowsAffected() == 0 {
			var previous string
			var encoded []byte
			if err := tx.QueryRow(ctx, `SELECT request_hash,response FROM api_requests WHERE scope=$1 AND key_hash=$2`, scope, keyHash).Scan(&previous, &encoded); err != nil {
				return DBError(err)
			}
			if previous != fingerprint {
				return status.Error(codes.AlreadyExists, "Idempotency-Key was already used with a different request")
			}
			if len(encoded) == 0 || proto.Unmarshal(encoded, response) != nil {
				return status.Error(codes.Internal, "stored idempotency response is unavailable")
			}
			return nil
		}
	}
	if err := create(ctx, tx); err != nil {
		return DBError(err)
	}
	if key != "" {
		encoded, err := (proto.MarshalOptions{Deterministic: true}).Marshal(response)
		if err != nil {
			return status.Error(codes.Internal, "encode idempotency response")
		}
		if _, err := tx.Exec(ctx, `UPDATE api_requests SET response=$3 WHERE scope=$1 AND key_hash=$2`, scope, keyHash, encoded); err != nil {
			return DBError(err)
		}
	}
	return DBError(tx.Commit(ctx))
}
