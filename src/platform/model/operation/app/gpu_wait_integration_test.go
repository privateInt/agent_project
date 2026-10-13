package app

import (
 "context"
 "testing"
 commonv1 "model-platform/gen/common/v1"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/operation/outbox"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5/pgxpool"
)
func gpuTestOperation(t *testing.T,pool *pgxpool.Pool)*operationv1.Operation{
 t.Helper();op,err:=New(pool).CreateOperation(context.Background(),&operationv1.CreateOperationRequest{Kind:"training.run",ExecutionType:"training.run",ResourceType:"training_run",ResourceId:uuid.NewString(),Resources:&commonv1.ExecutionResources{GpuCount:1,GpuIds:[]int32{0}}});if err!=nil{t.Fatal(err)};return op
}
func gpuWaitRequest(op *operationv1.Operation)*operationv1.DeferGPUDispatchRequest{return &operationv1.DeferGPUDispatchRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),Reason:"GPU reserved by contextrag-backend"}}
func makeGPUWaitDue(t *testing.T,pool *pgxpool.Pool,id string){t.Helper();if _,err:=pool.Exec(context.Background(),`UPDATE operations SET gpu_wait_until=clock_timestamp()-INTERVAL '1 second' WHERE operation_id=$1`,id);err!=nil{t.Fatal(err)}}
func TestGPUWaitDoesNotExhaustLaunchBudget(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool);id:=gpuTestOperation(t,pool).GetOperationId()
 for i:=int32(1);i<=5;i++ {
  op:=claimForGate(t,pool,id);if op.GetDispatchGeneration()!=i{t.Fatal("generation must remain monotonic")}
  waited,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));if err!=nil{t.Fatal(err)}
  if waited.GetStatus()!="pending"||waited.GetPhase()!="waiting_resources"||waited.GetErrorCode()!="GPU_BUSY"||waited.GetGpuWaitCount()!=i||waited.GetDispatchToken()!=""{t.Fatal(waited)}
  noClaim,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OperationId:id,OwnerId:"duplicate"});if err!=nil||noClaim.GetClaimed(){t.Fatal("wait deadline bypassed",err)}
  repeat,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));if err!=nil||repeat.GetGpuWaitCount()!=i||!repeat.GetGpuWaitUntil().AsTime().Equal(waited.GetGpuWaitUntil().AsTime()){t.Fatal("replay changed wait accounting",err)}
  _,err=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));expectGateDenied(t,err)
  var scheduled bool
  if err:=pool.QueryRow(ctx,`SELECT published_at IS NULL AND next_attempt_at=(SELECT gpu_wait_until FROM operations WHERE operation_id=$1) FROM operation_outbox WHERE operation_id=$1`,id).Scan(&scheduled);err!=nil||!scheduled{t.Fatal("future delivery was not committed",err)}
  worked,err:=outbox.New(pool,&fakePublisher{}).ProcessOne(ctx);if err!=nil||worked{t.Fatal("wait event published early",err)}
  makeGPUWaitDue(t,pool,id)
 }
 // Real uncertain launches retain their original limit of three.
 for attempt:=int32(1);attempt<=3;attempt++{
  op:=claimForGate(t,pool,id);if op.GetDispatchGeneration()-op.GetGpuWaitCount()!=attempt{t.Fatal(op)}
  expireGateLease(t,pool,id)
  _,err:=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:id,ExpectedDispatchToken:op.GetDispatchToken()})
  if attempt<3{if err!=nil{t.Fatal(err)}}else{expectGateDenied(t,err)}
 }
}
func TestGPUWaitRollbackAndFencing(t *testing.T){
 for _,failure:=range []string{"missing-outbox","outbox-write","operation-write"}{t.Run(failure,func(t *testing.T){
  pool:=outboxTestPool(t);svc:=New(pool);ctx:=context.Background();op:=claimForGate(t,pool,gpuTestOperation(t,pool).GetOperationId())
  if _,err:=pool.Exec(ctx,`UPDATE operation_outbox SET published_at=NOW()`);err!=nil{t.Fatal(err)}
  sql:=`DELETE FROM operation_outbox`
  if failure=="outbox-write"{sql=`ALTER TABLE operation_outbox ADD CONSTRAINT reject_wait CHECK(published_at IS NOT NULL)`}
  if failure=="operation-write"{sql=`ALTER TABLE operations ADD CONSTRAINT reject_wait CHECK(gpu_wait_count=0)`}
  if _,err:=pool.Exec(ctx,sql);err!=nil{t.Fatal(err)}
  if _,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));err==nil{t.Fatal("partial wait commit accepted")}
  current,err:=svc.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:op.GetOperationId()});if err!=nil{t.Fatal(err)}
  if current.GetDispatchToken()!=op.GetDispatchToken()||current.GetGpuWaitCount()!=0||current.GetStatus()!="dispatching"{t.Fatal("failed wait escaped rollback")}
 })}
 pool:=outboxTestPool(t);svc:=New(pool);ctx:=context.Background();op:=claimForGate(t,pool,gpuTestOperation(t,pool).GetOperationId())
 wrong:=gpuWaitRequest(op);wrong.DispatchToken=uuid.NewString();_,err:=svc.DeferGPUDispatch(ctx,wrong);expectGateDenied(t,err)
 if _,err:=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));err!=nil{t.Fatal(err)}
 _,err=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));expectGateDenied(t,err)
 cpu:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId());_,err=svc.DeferGPUDispatch(ctx,gpuWaitRequest(cpu));expectGateDenied(t,err)
}
func TestExpiredConfirmedGPUWaitCanBeExplicitlyRecoveredOnce(t *testing.T){
 pool:=outboxTestPool(t);svc:=New(pool);ctx:=context.Background();id:=gpuTestOperation(t,pool).GetOperationId()
 var last *operationv1.Operation
 for i:=0;i<3;i++{last=claimForGate(t,pool,id);expireGateLease(t,pool,id);if i<2{if _,err:=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:id,ExpectedDispatchToken:last.GetDispatchToken()});err!=nil{t.Fatal(err)}}}
 if _,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(last));err!=nil{t.Fatal(err)}
 makeGPUWaitDue(t,pool,id);next:=claimForGate(t,pool,id)
 if next.GetDispatchGeneration()!=4||next.GetGpuWaitCount()!=1{t.Fatal("recovery reset unrelated failures")}
 if _,err:=svc.BeginExecution(ctx,gateRequest(next,uuid.NewString()));err!=nil{t.Fatal(err)}
 _,err:=svc.BeginExecution(ctx,gateRequest(last,uuid.NewString()));expectGateDenied(t,err)
 // Late wait acknowledgement is idempotent and cannot revoke a newer grant.
 replay,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(last));if err!=nil||replay.GetStatus()!="running"||replay.GetGpuWaitCount()!=1{t.Fatal("late wait changed new execution",err)}
}
func TestGPUWaitRacesExecutionGrant(t *testing.T){
 pool:=outboxTestPool(t);svc:=New(pool);ctx:=context.Background();op:=claimForGate(t,pool,gpuTestOperation(t,pool).GetOperationId())
 start:=make(chan struct{});results:=make(chan error,2)
 go func(){<-start;_,err:=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));results<-err}()
 go func(){<-start;_,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));results<-err}()
 close(start);first,second:=<-results,<-results
 if (first==nil)==(second==nil){t.Fatalf("grant and wait must not both commit: %v / %v",first,second)}
 if first!=nil{expectGateDenied(t,first)};if second!=nil{expectGateDenied(t,second)}
}

func TestGPUWaitOutboxAllowsOtherOperationsAndLaterRedelivery(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
 op:=claimForGate(t,pool,gpuTestOperation(t,pool).GetOperationId())
 var eventBefore,eventAfter string
 if err:=pool.QueryRow(ctx,`SELECT event_id FROM operation_outbox WHERE operation_id=$1`,op.GetOperationId()).Scan(&eventBefore);err!=nil{t.Fatal(err)}
 if _,err:=svc.DeferGPUDispatch(ctx,gpuWaitRequest(op));err!=nil{t.Fatal(err)}
 other:=createTestOperation(t,pool);publisher:=&fakePublisher{};worker:=outbox.New(pool,publisher)
 if worked,err:=worker.ProcessOne(ctx);err!=nil||!worked{t.Fatal("unrelated event was blocked",err)}
 if len(publisher.calls)!=1||publisher.calls[0].OperationID!=other.GetOperationId(){t.Fatal("wrong event published during wait")}
 if _,err:=pool.Exec(ctx,`UPDATE operation_outbox SET next_attempt_at=clock_timestamp()-INTERVAL '1 second' WHERE operation_id=$1`,op.GetOperationId());err!=nil{t.Fatal(err)}
 makeGPUWaitDue(t,pool,op.GetOperationId())
 if worked,err:=worker.ProcessOne(ctx);err!=nil||!worked{t.Fatal("due wait was not redelivered",err)}
 if len(publisher.calls)!=2||publisher.calls[1].OperationID!=op.GetOperationId(){t.Fatal("GPU request was lost")}
 if err:=pool.QueryRow(ctx,`SELECT event_id FROM operation_outbox WHERE operation_id=$1`,op.GetOperationId()).Scan(&eventAfter);err!=nil||eventAfter!=eventBefore{t.Fatal("wait replaced event identity",err)}
}
