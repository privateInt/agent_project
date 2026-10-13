// Package outbox delivers committed commands at least once. It never retries
// computation, revives terminal operations, or recovers legacy operations.
package outbox

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "log"
 "time"

 "github.com/jackc/pgx/v5"
 "github.com/jackc/pgx/v5/pgxpool"
 "knowledge-platform/internal/execution"
)

func InitSchema(ctx context.Context, pool *pgxpool.Pool) error {
 _,err:=pool.Exec(ctx, `CREATE TABLE IF NOT EXISTS operation_outbox (
 event_id UUID PRIMARY KEY,
 operation_id UUID NOT NULL UNIQUE REFERENCES operations(operation_id),
 payload JSONB NOT NULL,
 publish_attempts INTEGER NOT NULL DEFAULT 0 CHECK(publish_attempts>=0),
 next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 last_error TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 published_at TIMESTAMPTZ
 );
 CREATE INDEX IF NOT EXISTS operation_outbox_due_idx
 ON operation_outbox(next_attempt_at,created_at) WHERE published_at IS NULL;`)
 return err
}

type Publisher interface { Publish(context.Context, execution.Command) error }
type Worker struct { pool *pgxpool.Pool; publisher Publisher }
func New(pool *pgxpool.Pool,publisher Publisher)*Worker {return &Worker{pool:pool,publisher:publisher}}

// Retry publish indefinitely with a capped delay; max_attempts on Operation
// remains a computation-attempt budget and is unrelated to Kafka availability.
func retryDelay(attempt int) time.Duration {
 if attempt < 1 {attempt=1}
 if attempt > 6 {return 60*time.Second}
 return time.Duration(1<<uint(attempt-1))*time.Second
}

func(w *Worker) Run(ctx context.Context) {
 log.Print("operation outbox publisher started")
 for ctx.Err()==nil {
  worked,err:=w.ProcessOne(ctx)
  if err!=nil {log.Printf("outbox processing error: %v",err)}
  if worked && err==nil {continue}
  timer:=time.NewTimer(time.Second)
  select {case <-ctx.Done():timer.Stop();return;case <-timer.C:}
 }
}

// Hold only one outbox row lock across a bounded Kafka call. SKIP LOCKED permits
// multiple publishers. A crash after Kafka acknowledgement and before commit
// leaves the event pending: the SAME event ID is republished. Dispatcher Claim
// prevents duplicate claims. An expired, unstarted dispatch can be rearmed by
// Reconciler; BeginExecution fences old tokens before any domain work starts.
func(w *Worker) ProcessOne(parent context.Context)(bool,error) {
 ctx,cancel:=context.WithTimeout(parent,10*time.Second);defer cancel()
 tx,err:=w.pool.Begin(ctx);if err!=nil{return false,err}
 defer func() { cleanup, stop := context.WithTimeout(context.Background(), 2*time.Second); defer stop(); _ = tx.Rollback(cleanup) }()
 var eventID,operationID string
 var payload []byte
 var attempts int
 err=tx.QueryRow(ctx,`SELECT event_id,operation_id,payload,publish_attempts
 FROM operation_outbox WHERE published_at IS NULL AND next_attempt_at<=NOW()
 ORDER BY next_attempt_at,created_at,event_id LIMIT 1 FOR UPDATE SKIP LOCKED`).Scan(&eventID,&operationID,&payload,&attempts)
 if errors.Is(err,pgx.ErrNoRows){return false,nil}
 if err!=nil{return false,err}
 var cmd execution.Command
 publishErr:=json.Unmarshal(payload,&cmd)
 if publishErr==nil && (cmd.EventID!=eventID || cmd.OperationID!=operationID || cmd.ExecutionType=="" || cmd.ResourceID=="") {
  publishErr=errors.New("outbox payload identity or routing mismatch")
 }
 if publishErr==nil {
  publishCtx,stop:=context.WithTimeout(ctx,5*time.Second)
  publishErr=w.publisher.Publish(publishCtx,cmd)
  stop()
 }
 if publishErr!=nil {
  // Bounded diagnostic text prevents a broker error from growing this row.
  message:=[]rune(publishErr.Error());if len(message)>2000 {message=message[:2000]}
  _,err=tx.Exec(ctx,`UPDATE operation_outbox SET publish_attempts=publish_attempts+1,
  next_attempt_at=NOW()+($2::bigint * INTERVAL '1 second'),last_error=$3 WHERE event_id=$1`,eventID,int64(retryDelay(attempts+1)/time.Second),string(message))
 } else {
  _,err=tx.Exec(ctx,`UPDATE operation_outbox SET publish_attempts=publish_attempts+1,published_at=NOW(),last_error='' WHERE event_id=$1`,eventID)
 }
 if err!=nil{return true,fmt.Errorf("persist publish result event=%s: %w",eventID,err)}
 if err=tx.Commit(ctx);err!=nil{return true,fmt.Errorf("commit publish result event=%s: %w",eventID,err)}
 if publishErr!=nil {log.Printf("outbox publish deferred event=%s operation=%s attempt=%d: %v",eventID,operationID,attempts+1,publishErr)}
 return true,nil
}
