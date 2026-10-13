package app
import(
 "context"
 "testing"
 "github.com/jackc/pgx/v5/pgxpool"
 operationv1 "data-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
func TestReconcileCompareAndSwap(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
 op:=createTestOperation(t,pool)
 snapshot:=legacyReconcileFixture(t,pool,op.GetOperationId())
 req:=&operationv1.ApplyReconciliationRequest{OperationId:snapshot.GetOperationId(),ExpectedStatus:snapshot.GetStatus(),ExpectedAttempt:snapshot.GetAttempt(),ExpectedUpdatedAt:snapshot.GetUpdatedAt(),TargetStatus:"running",Note:"matching container is running"}
 repaired,err:=svc.ApplyReconciliation(ctx,req);if err!=nil{t.Fatal(err)}
 if repaired.GetStatus()!="running"{t.Fatal("not repaired")}
 if _,err=svc.ApplyReconciliation(ctx,req);status.Code(err)!=codes.FailedPrecondition{t.Fatalf("accepted old snapshot: %v",err)}
 req.ExpectedStatus=repaired.GetStatus();req.ExpectedUpdatedAt=repaired.GetUpdatedAt();req.TargetStatus="succeeded";req.Note="domain completion confirmed"
 if _,err=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId()});err!=nil{t.Fatal(err)}
 if _,err=svc.ApplyReconciliation(ctx,req);status.Code(err)!=codes.FailedPrecondition{t.Fatalf("overwrote concurrent completion: %v",err)}
}
func TestReconcileObservationPreservesState(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool);op:=createTestOperation(t,pool)
 snapshot:=legacyReconcileFixture(t,pool,op.GetOperationId())
 snap:=snapshot
 got,err:=svc.ApplyReconciliation(ctx,&operationv1.ApplyReconciliationRequest{OperationId:snap.GetOperationId(),ExpectedStatus:snap.GetStatus(),ExpectedAttempt:snap.GetAttempt(),ExpectedUpdatedAt:snap.GetUpdatedAt(),Note:"manual review: container missing"});if err!=nil{t.Fatal(err)}
 if got.GetStatus()!=snap.GetStatus()||!got.GetUpdatedAt().AsTime().Equal(snap.GetUpdatedAt().AsTime()){t.Fatal("observation changed lifecycle")}
 var note string
 if err=pool.QueryRow(ctx,`SELECT reconcile_note FROM operations WHERE operation_id=$1`,op.GetOperationId()).Scan(&note);err!=nil||note==""{t.Fatalf("note missing: %v",err)}
}
func TestReconcileCandidateAge(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool);op:=createTestOperation(t,pool)
 if _,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OwnerId:"test-dispatcher",OperationId:op.GetOperationId()});err!=nil{t.Fatal(err)}
 list,err:=svc.ListReconcileOperations(ctx,&operationv1.ListReconcileOperationsRequest{});if err!=nil{t.Fatal(err)}
 if len(list.GetOperations())!=0{t.Fatal("fresh operation selected")}
 if _,err=pool.Exec(ctx,`UPDATE operations SET updated_at=NOW()-INTERVAL '2 minutes' WHERE operation_id=$1`,op.GetOperationId());err!=nil{t.Fatal(err)}
 list,err=svc.ListReconcileOperations(ctx,&operationv1.ListReconcileOperationsRequest{});if err!=nil{t.Fatal(err)}
 if len(list.GetOperations())!=1{t.Fatal("stale operation not selected")}
}
func TestReconcileTerminalRepairs(t *testing.T){
 for _,target:=range []string{"succeeded","failed"}{t.Run(target,func(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool);op:=createTestOperation(t,pool)
 snapshot:=legacyReconcileFixture(t,pool,op.GetOperationId())
 snap:=snapshot;got,err:=svc.ApplyReconciliation(ctx,&operationv1.ApplyReconciliationRequest{OperationId:snap.GetOperationId(),ExpectedStatus:snap.GetStatus(),ExpectedAttempt:snap.GetAttempt(),ExpectedUpdatedAt:snap.GetUpdatedAt(),TargetStatus:target,Note:"domain terminal result"});if err!=nil{t.Fatal(err)}
 if got.GetStatus()!=target||got.GetCompletedAt()==nil{t.Fatal("terminal repair missing")}
 if target=="failed"&&got.GetErrorCode()!="DOMAIN_REPORTED_FAILURE"{t.Fatal("failure reason missing")}
 })}
}

// Existing rows retain protocol=0 during schema migration. These fixtures test
// that the original reconciliation/CAS behavior remains available for them.
func legacyReconcileFixture(t *testing.T,pool *pgxpool.Pool,id string)*operationv1.Operation{
 t.Helper()
 ctx:=context.Background()
 if _,err:=pool.Exec(ctx,`UPDATE operations SET dispatch_protocol=0,status='dispatching',phase='dispatching',attempt=1,started_at=NOW(),updated_at=NOW() WHERE operation_id=$1`,id);err!=nil{t.Fatal(err)}
 op,err:=New(pool).GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:id});if err!=nil{t.Fatal(err)}
 return op
}
