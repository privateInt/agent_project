package app

import (
 "context"
 "errors"
 "os"
 "testing"

 "github.com/google/uuid"
 "github.com/jackc/pgx/v5/pgxpool"
 operationv1 "data-platform/gen/operation/v1"
 "data-platform/internal/execution"
 "data-platform/operation/outbox"
)

type fakePublisher struct { calls []execution.Command; err error }
func(p *fakePublisher) Publish(_ context.Context,cmd execution.Command)error {p.calls=append(p.calls,cmd);return p.err}

func outboxTestPool(t *testing.T)*pgxpool.Pool {
 t.Helper()
 dsn:=os.Getenv("DATA_TEST_POSTGRES_URL");if dsn=="" {t.Skip("DATA_TEST_POSTGRES_URL is not set")}
 ctx:=context.Background()
 admin,err:=pgxpool.New(ctx,dsn);if err!=nil {t.Fatal(err)}
 t.Cleanup(admin.Close)
 schema:="outbox_test_"+uuid.NewString()[:8]
 if _,err=admin.Exec(ctx,"CREATE SCHEMA "+schema);err!=nil {t.Fatal(err)}
 t.Cleanup(func(){if _,e:=admin.Exec(ctx,"DROP SCHEMA "+schema+" CASCADE");e!=nil {t.Errorf("cleanup: %v",e)}})
 cfg,err:=pgxpool.ParseConfig(dsn);if err!=nil {t.Fatal(err)}
 cfg.ConnConfig.RuntimeParams["search_path"]=schema
 pool,err:=pgxpool.NewWithConfig(ctx,cfg);if err!=nil {t.Fatal(err)}
 t.Cleanup(pool.Close)
 if err=InitSchema(ctx,pool);err!=nil {t.Fatal(err)}
 return pool
}
func createTestOperation(t *testing.T,pool *pgxpool.Pool)*operationv1.Operation {
 t.Helper()
 op,err:=New(pool).CreateOperation(context.Background(),&operationv1.CreateOperationRequest{Kind:"test",ExecutionType:"test",ResourceType:"test",ResourceId:uuid.NewString()})
 if err!=nil {t.Fatal(err)}
 return op
}

func TestOutboxAtomicRollback(t *testing.T) {
 pool:=outboxTestPool(t);ctx:=context.Background()
 // Force the second INSERT to fail. The first INSERT must also disappear.
 if _,err:=pool.Exec(ctx,`ALTER TABLE operation_outbox ADD CONSTRAINT reject_events CHECK(false) NOT VALID`);err!=nil {t.Fatal(err)}
 _,err:=New(pool).CreateOperation(ctx,&operationv1.CreateOperationRequest{Kind:"test",ExecutionType:"test",ResourceType:"test",ResourceId:uuid.NewString()})
 if err==nil {t.Fatal("expected outbox write failure")}
 var count int
 if err=pool.QueryRow(ctx,`SELECT COUNT(*) FROM operations`).Scan(&count);err!=nil {t.Fatal(err)}
 if count!=0 {t.Fatal("operation escaped rolled back transaction")}
}

func TestOutboxOfflineThenRecovery(t *testing.T) {
 pool:=outboxTestPool(t);ctx:=context.Background()
 op:=createTestOperation(t,pool)
 pub:=&fakePublisher{err:errors.New("broker offline")}
 worker:=outbox.New(pool,pub)
 if worked,err:=worker.ProcessOne(ctx);!worked || err!=nil {t.Fatalf("worked=%v err=%v",worked,err)}
 var pending bool;var attempts int;var last string;var scheduled bool
 if err:=pool.QueryRow(ctx,`SELECT published_at IS NULL,publish_attempts,last_error,next_attempt_at>NOW() FROM operation_outbox WHERE operation_id=$1`,op.GetOperationId()).Scan(&pending,&attempts,&last,&scheduled);err!=nil {t.Fatal(err)}
 if !pending || attempts!=1 || last=="" || !scheduled {t.Fatalf("bad retry state: %v %d %q %v",pending,attempts,last,scheduled)}
 current,err:=New(pool).GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:op.GetOperationId()});if err!=nil {t.Fatal(err)}
 if current.GetStatus()!="pending" || current.GetAttempt()!=0 {t.Fatal("publish failure changed computation state")}
 // Simulate a fresh publisher after a process restart, using the persisted row.
 if _,err=pool.Exec(ctx,`UPDATE operation_outbox SET next_attempt_at=NOW()`);err!=nil {t.Fatal(err)}
 recovered:=&fakePublisher{}
 if worked,err:=outbox.New(pool,recovered).ProcessOne(ctx);!worked || err!=nil {t.Fatalf("worked=%v err=%v",worked,err)}
 if len(recovered.calls)!=1 || recovered.calls[0].EventID=="" || recovered.calls[0].EventID!=pub.calls[0].EventID {t.Fatal("event identity changed across retry")}
 if worked,err:=outbox.New(pool,recovered).ProcessOne(ctx);worked || err!=nil {t.Fatalf("published event repeated: %v %v",worked,err)}
 if err=pool.QueryRow(ctx,`SELECT published_at IS NULL,publish_attempts,last_error FROM operation_outbox`).Scan(&pending,&attempts,&last);err!=nil {t.Fatal(err)}
 if pending || attempts!=2 || last!="" {t.Fatal("publish acknowledgement not persisted")}
}

func TestOutboxAcknowledgementGap(t *testing.T) {
 pool:=outboxTestPool(t);ctx:=context.Background();op:=createTestOperation(t,pool)
 // Kafka succeeds but persisting the acknowledgement fails. This models the
 // same durable state as a crash before the acknowledgement transaction commits.
 if _,err:=pool.Exec(ctx,`ALTER TABLE operation_outbox ADD CONSTRAINT reject_ack CHECK(published_at IS NULL)`);err!=nil {t.Fatal(err)}
 pub:=&fakePublisher{};worker:=outbox.New(pool,pub)
 if _,err:=worker.ProcessOne(ctx);err==nil {t.Fatal("expected acknowledgement persistence failure")}
 if _,err:=pool.Exec(ctx,`ALTER TABLE operation_outbox DROP CONSTRAINT reject_ack`);err!=nil {t.Fatal(err)}
 if _,err:=worker.ProcessOne(ctx);err!=nil {t.Fatal(err)}
 if len(pub.calls)!=2 || pub.calls[0].EventID!=pub.calls[1].EventID {t.Fatal("expected duplicate delivery with same event ID")}
 svc:=New(pool)
 first,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OwnerId:"test-dispatcher",OperationId:op.GetOperationId()});if err!=nil {t.Fatal(err)}
 second,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OwnerId:"test-dispatcher",OperationId:op.GetOperationId()});if err!=nil {t.Fatal(err)}
 if !first.GetClaimed() || second.GetClaimed() || second.GetOperation().GetAttempt()!=1 {t.Fatal("duplicate command was claimed twice")}
}

func TestOutboxSkipsLockedRow(t *testing.T) {
 pool:=outboxTestPool(t);ctx:=context.Background();createTestOperation(t,pool)
 tx,err:=pool.Begin(ctx);if err!=nil {t.Fatal(err)};defer tx.Rollback(ctx)
 var id string
 if err=tx.QueryRow(ctx,`SELECT event_id FROM operation_outbox FOR UPDATE`).Scan(&id);err!=nil {t.Fatal(err)}
 pub:=&fakePublisher{}
 if worked,err:=outbox.New(pool,pub).ProcessOne(ctx);worked || err!=nil {t.Fatalf("locked event picked up: %v %v",worked,err)}
 if len(pub.calls)!=0 {t.Fatal("published a locked event")}
}
