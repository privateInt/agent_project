package app

import (
 "context"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

func(s *Service) RequestTrainingCancellation(ctx context.Context,r *operationv1.TrainingCancellationRequest)(*operationv1.Operation,error){
 return s.trainingCancellation(ctx,r,false)
}
func(s *Service) ConfirmTrainingCancellation(ctx context.Context,r *operationv1.TrainingCancellationRequest)(*operationv1.Operation,error){
 return s.trainingCancellation(ctx,r,true)
}
func(s *Service) trainingCancellation(ctx context.Context,r *operationv1.TrainingCancellationRequest,confirm bool)(*operationv1.Operation,error){
 if !validID(r.GetOperationId()) || !validID(r.GetTrainingRunId()) {return nil,status.Error(codes.InvalidArgument,"valid operation and training run IDs required")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Error(codes.Unavailable,"begin training cancellation")};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,r.GetOperationId());if err!=nil{return nil,err}
 if op.GetExecutionType()!="training.run" || op.GetResourceType()!="training_run" || op.GetResourceId()!=r.GetTrainingRunId() || op.GetDispatchProtocol()!=1 {return nil,status.Error(codes.FailedPrecondition,"training execution identity mismatch")}
 if confirm && op.GetDispatchToken()!=r.GetExpectedDispatchToken() {return nil,status.Error(codes.FailedPrecondition,"cancellation generation changed")}
 switch op.GetStatus(){case "succeeded","failed","cancelled":return op,nil}
 if confirm {
  if op.GetStatus()!="cancelling" {return nil,status.Error(codes.FailedPrecondition,"cancellation has not been requested")}
  _,err=tx.Exec(ctx,`UPDATE operations SET status='cancelled',phase='cancelled',completed_at=clock_timestamp(),updated_at=clock_timestamp(),error_code='',error_message='',retryable=FALSE WHERE operation_id=$1`,op.GetOperationId())
 }else{
  if op.GetStatus()=="cancelling" {return op,nil}
  // The token is retained for ownership/log lookup but no longer grants execution.
  _,err=tx.Exec(ctx,`UPDATE operations SET status='cancelling',phase='cancelling',dispatch_lease_until=NULL,gpu_wait_until=NULL,error_code='',error_message='',retryable=FALSE,updated_at=clock_timestamp() WHERE operation_id=$1`,op.GetOperationId())
 }
 if err!=nil{return nil,status.Error(codes.Internal,"save training cancellation")}
 return commitOperation(ctx,tx,op.GetOperationId())
}
