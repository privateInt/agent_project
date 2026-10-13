package app

import (
 "context"
 "testing"
 "github.com/google/uuid"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
func TestTrainingCancellationFencesAllExecutionPaths(t *testing.T){
 for _,initial:=range []string{"pending","dispatching","running"}{t.Run(initial,func(t *testing.T){
  pool:=outboxTestPool(t);svc:=New(pool);ctx:=context.Background()
  op,err:=svc.CreateOperation(ctx,&operationv1.CreateOperationRequest{Kind:"training.run",ExecutionType:"training.run",ResourceType:"training_run",ResourceId:uuid.NewString()});if err!=nil{t.Fatal(err)}
  if initial!="pending"{op=claimForGate(t,pool,op.GetOperationId())}
  instance:=uuid.NewString()
  if initial=="running"{op,err=svc.BeginExecution(ctx,gateRequest(op,instance));if err!=nil{t.Fatal(err)}}
  req:=&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId(),ExpectedDispatchToken:op.GetDispatchToken()}
  _,err=svc.ConfirmTrainingCancellation(ctx,req);expectGateDenied(t,err)
  requested,err:=svc.RequestTrainingCancellation(ctx,req);if err!=nil||requested.GetStatus()!="cancelling"{t.Fatal(requested,err)}
  again,err:=svc.RequestTrainingCancellation(ctx,req);if err!=nil||!again.GetUpdatedAt().AsTime().Equal(requested.GetUpdatedAt().AsTime()){t.Fatal("replay changed first fence",err)}
  claim,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OperationId:op.GetOperationId(),OwnerId:"late"});if err!=nil||claim.GetClaimed(){t.Fatal("claim passed cancellation",err)}
  if initial!="pending"{
   _,err=svc.BeginExecution(ctx,gateRequest(op,instance));expectGateDenied(t,err)
   _,err=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),ExecutionInstanceId:instance});expectGateDenied(t,err)
   _,err=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:op.GetOperationId(),ExpectedDispatchToken:op.GetDispatchToken()});expectGateDenied(t,err)
  }
  wrong:=&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId(),ExpectedDispatchToken:uuid.NewString()};_,err=svc.ConfirmTrainingCancellation(ctx,wrong);expectGateDenied(t,err)
  done,err:=svc.ConfirmTrainingCancellation(ctx,req);if err!=nil||done.GetStatus()!="cancelled"||done.GetCompletedAt()==nil{t.Fatal(done,err)}
  replay,err:=svc.ConfirmTrainingCancellation(ctx,req);if err!=nil||!replay.GetCompletedAt().AsTime().Equal(done.GetCompletedAt().AsTime()){t.Fatal("cancel completion was not idempotent",err)}
 })}
}
func TestTrainingCancellationPreservesTerminalAndRejectsWrongResource(t *testing.T){
 pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
 for _,state:=range []string{"succeeded","failed"}{
  op,err:=svc.CreateOperation(ctx,&operationv1.CreateOperationRequest{Kind:"training.run",ExecutionType:"training.run",ResourceType:"training_run",ResourceId:uuid.NewString()});if err!=nil{t.Fatal(err)}
  if _,err=pool.Exec(ctx,`UPDATE operations SET status=$2,completed_at=NOW() WHERE operation_id=$1`,op.GetOperationId(),state);err!=nil{t.Fatal(err)}
  got,err:=svc.RequestTrainingCancellation(ctx,&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId()});if err!=nil||got.GetStatus()!=state{t.Fatal("terminal outcome changed",err)}
  _,err=svc.RequestTrainingCancellation(ctx,&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:uuid.NewString()});if status.Code(err)!=codes.FailedPrecondition{t.Fatal(err)}
 }
 op:=createTestOperation(t,pool);_,err:=svc.RequestTrainingCancellation(ctx,&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId()});expectGateDenied(t,err)
}
