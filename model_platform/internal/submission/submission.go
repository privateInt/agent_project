// Package submission is the durable domain -> Operation creation handoff.
// Its queue lives in each domain's own DB, alongside that domain's resources.
package submission

import (
	"context"
	"errors"
	"fmt"
	"log"
	"time"

	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/idempotency"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

const Schema = `
CREATE TABLE IF NOT EXISTS operation_submissions (
 operation_id UUID PRIMARY KEY,
 resource_type TEXT NOT NULL,
 resource_id UUID NOT NULL,
 request BYTEA NOT NULL,
 status VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','delivered','blocked')),
 attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 last_error_code TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 delivered_at TIMESTAMPTZ,
 UNIQUE(resource_type,resource_id)
);
CREATE INDEX IF NOT EXISTS operation_submissions_pending_idx ON operation_submissions(next_attempt_at,created_at) WHERE status='pending';
`

type Ensure func(context.Context, *operationv1.CreateOperationRequest) (*operationv1.Operation, error)

type Worker struct {
	Pool *pgxpool.Pool
	Ensure Ensure
}

func Enqueue(ctx context.Context, tx pgx.Tx, req *operationv1.CreateOperationRequest) error {
	id, err := uuid.Parse(req.GetOperationId())
	if err != nil || id == uuid.Nil {
		return fmt.Errorf("submission requires a reserved operation ID")
	}
	data, err := proto.Marshal(req)
	if err != nil {
		return err
	}
	_, err = tx.Exec(ctx, `INSERT INTO operation_submissions(operation_id,resource_type,resource_id,request) VALUES($1,$2,$3,$4)`, req.GetOperationId(), req.GetResourceType(), req.GetResourceId(), data)
	return err
}

func retryDelay(attempt int) time.Duration {
	if attempt < 1 {
		attempt = 1
	}
	if attempt > 6 {
		return time.Minute
	}
	return time.Duration(1<<uint(attempt-1))*time.Second
}

func permanent(code codes.Code) bool {
	switch code {
	case codes.InvalidArgument, codes.AlreadyExists, codes.FailedPrecondition, codes.PermissionDenied, codes.Unauthenticated:
		return true
	default:
		return false
	}
}

// Deliver locks one due item through its bounded RPC and acknowledgement.
// A crash rolls back only the local acknowledgement: EnsureOperation safely
// repeats the same operation ID if the remote commit already succeeded.
func (w Worker) Deliver(ctx context.Context, operationID string) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	tx, err := w.Pool.Begin(ctx)
	if err != nil {
		return false, err
	}
	defer idempotency.Rollback(tx)
	var id string
	var data []byte
	var attempts int
	err = tx.QueryRow(ctx, `SELECT operation_id,request,attempts FROM operation_submissions
WHERE status='pending' AND next_attempt_at<=clock_timestamp() AND ($1::text='' OR operation_id::text=$1::text)
ORDER BY next_attempt_at,created_at,operation_id LIMIT 1 FOR UPDATE SKIP LOCKED`, operationID).Scan(&id, &data, &attempts)
	if errors.Is(err, pgx.ErrNoRows) {
		return false, nil
	}
	if err != nil {
		return false, err
	}
	var req operationv1.CreateOperationRequest
	code := codes.OK
	if proto.Unmarshal(data, &req) != nil || req.GetOperationId() != id {
		code = codes.InvalidArgument
	} else {
		rpcCtx, rpcCancel := context.WithTimeout(ctx, 3*time.Second)
		op, callErr := w.Ensure(rpcCtx, &req)
		rpcCancel()
		code = status.Code(callErr)
		if code == codes.OK && (op.GetOperationId() != id || op.GetResourceId() != req.GetResourceId() || op.GetExecutionType() != req.GetExecutionType()) {
			code = codes.FailedPrecondition
		}
	}
	attempts++
	if code == codes.OK {
		_, err = tx.Exec(ctx, `UPDATE operation_submissions SET status='delivered',attempts=$2,last_error_code='',delivered_at=clock_timestamp() WHERE operation_id=$1`, id, attempts)
	} else if permanent(code) {
		// Do not fail/delete a domain row on an ambiguous remote outcome.
		// Expose blocked delivery for diagnosis without inventing a Job failure.
		_, err = tx.Exec(ctx, `UPDATE operation_submissions SET status='blocked',attempts=$2,last_error_code=$3 WHERE operation_id=$1`, id, attempts, code.String())
	} else {
		_, err = tx.Exec(ctx, `UPDATE operation_submissions SET attempts=$2,last_error_code=$3,next_attempt_at=clock_timestamp()+make_interval(secs => $4::double precision) WHERE operation_id=$1`, id, attempts, code.String(), retryDelay(attempts).Seconds())
	}
	if err != nil {
		return true, err
	}
	if err := tx.Commit(ctx); err != nil {
		return true, err
	}
	if code != codes.OK {
		log.Printf("operation submission deferred operation=%s code=%s blocked=%t", id, code, permanent(code))
	}
	return true, nil
}

func (w Worker) Try(ctx context.Context, id string) {
	if _, err := w.Deliver(ctx, id); err != nil {
		// Persisted submission survives response cancellation or process restart.
		log.Printf("operation submission acknowledgement unavailable operation=%s", id)
	}
}

func (w Worker) Run(ctx context.Context) {
	log.Print("operation submission worker started")
	for ctx.Err() == nil {
		for i := 0; i < 20; i++ {
			found, err := w.Deliver(ctx, "")
			if err != nil {
				log.Print("operation submission cycle unavailable"); break
			}
			if !found {
				break
			}
		}
		select { case <-ctx.Done(): return; case <-time.After(time.Second): }
	}
}

type Info struct { OperationID, Status, ErrorCode string }

func Lookup(ctx context.Context, pool *pgxpool.Pool, resourceType, resourceID string) (Info, error) {
	var info Info
	err := pool.QueryRow(ctx, `SELECT operation_id,status,last_error_code FROM operation_submissions WHERE resource_type=$1 AND resource_id=$2`, resourceType, resourceID).Scan(&info.OperationID, &info.Status, &info.ErrorCode)
	if errors.Is(err, pgx.ErrNoRows) {
		return info, nil
	}
	return info, err
}
