package app

import (
 "context"
 "os"
 "testing"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5/pgxpool"
 operationv1 "data-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

// Uses a disposable schema only. Set DATA_TEST_POSTGRES_URL to opt in.
func TestTransitionDatabase(t *testing.T) {
 dsn:=os.Getenv("DATA_TEST_POSTGRES_URL")
 if dsn=="" {t.Skip("DATA_TEST_POSTGRES_URL is not set")}
 ctx:=context.Background()
 admin,err:=pgxpool.New(ctx,dsn);if err!=nil {t.Fatal(err)};defer admin.Close()
 schema:="op_test_"+uuid.New().String()[:8]
 if _,err=admin.Exec(ctx,"CREATE SCHEMA "+schema);err!=nil {t.Fatal(err)}
 defer admin.Exec(ctx,"DROP SCHEMA "+schema+" CASCADE")
 cfg,err:=pgxpool.ParseConfig(dsn);if err!=nil {t.Fatal(err)}
 cfg.ConnConfig.RuntimeParams["search_path"]=schema
 pool,err:=pgxpool.NewWithConfig(ctx,cfg);if err!=nil {t.Fatal(err)};defer pool.Close()
 if err=InitSchema(ctx,pool);err!=nil {t.Fatal(err)}
 svc:=New(pool)
 id:=uuid.NewString()
 _,err=pool.Exec(ctx,`INSERT INTO operations(operation_id,kind,execution_type,resource_type,resource_id,status,phase) VALUES($1,'test','test','test',$2,'dispatching','dispatching')`,id,uuid.NewString())
 if err!=nil {t.Fatal(err)}
 if _,err=svc.MarkRunning(ctx,&operationv1.UpdateOperationRequest{OperationId:id});err!=nil {t.Fatal(err)}
 first,err:=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:id,Phase:"completed"});if err!=nil {t.Fatal(err)}
 second,err:=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:id,Phase:"late"});if err!=nil {t.Fatal(err)}
 if second.GetPhase()!="completed" || !first.GetCompletedAt().AsTime().Equal(second.GetCompletedAt().AsTime()) {t.Fatal("duplicate changed completed outcome")}
 if _,err=svc.MarkRunning(ctx,&operationv1.UpdateOperationRequest{OperationId:id});status.Code(err)!=codes.FailedPrecondition {t.Fatalf("revival: %v",err)}
 if _,err=svc.FailOperation(ctx,&operationv1.FailOperationRequest{OperationId:id});status.Code(err)!=codes.FailedPrecondition {t.Fatalf("overwrite: %v",err)}
 if _,err=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:uuid.NewString()});status.Code(err)!=codes.NotFound {t.Fatalf("missing: %v",err)}
 // A genuine database write error must reach the caller.
 _,err=pool.Exec(ctx,`UPDATE operations SET status='running',completed_at=NULL WHERE operation_id=$1`,id);if err!=nil {t.Fatal(err)}
 _,err=pool.Exec(ctx,`ALTER TABLE operations ADD CONSTRAINT reject_success CHECK(status<>'succeeded')`);if err!=nil {t.Fatal(err)}
 if _,err=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:id});status.Code(err)!=codes.Internal {t.Fatalf("write error suppressed: %v",err)}
}
