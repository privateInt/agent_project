package app

import (
 "context"
 "strings"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

// DeferGPUDispatch records a trusted Dispatcher report that Docker creation was
// never invoked. Unknown creation results MUST continue through RecoverDispatch.
// An explicit operator recovery can use an expired current token only after the
// shared admission lock proves there is no creation claim or Job for this op.
func(s *Service)DeferGPUDispatch(ctx context.Context,r *operationv1.DeferGPUDispatchRequest)(*operationv1.Operation,error){
 reason:=strings.TrimSpace(r.GetReason())
 if !validID(r.GetOperationId())||!validID(r.GetDispatchToken())||reason==""||len(reason)>1000{return nil,status.Error(codes.InvalidArgument,"operation, token and bounded GPU wait reason required")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin GPU wait: %v",err)};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,r.GetOperationId());if err!=nil{return nil,err}
 var lastToken string
 if err:=tx.QueryRow(ctx,`SELECT COALESCE(gpu_wait_token::text,'') FROM operations WHERE operation_id=$1`,op.GetOperationId()).Scan(&lastToken);err!=nil{return nil,status.Errorf(codes.Internal,"read GPU wait replay: %v",err)}
 // A lost acknowledgement cannot add another credit or postpone the deadline.
 if lastToken==r.GetDispatchToken(){return op,nil}
 if op.GetDispatchProtocol()!=1||op.GetStatus()!="dispatching"||op.GetDispatchToken()!=r.GetDispatchToken()||op.GetExecutionStartedAt()!=nil||op.GetResources().GetGpuCount()<1||op.GetGpuWaitCount()>=op.GetDispatchGeneration(){return nil,status.Error(codes.FailedPrecondition,"GPU wait requires the current unstarted GPU dispatch")}
 // Locking the outbox row also serializes with an in-flight publisher ACK.
 tag,err:=tx.Exec(ctx,`UPDATE operation_outbox SET published_at=NULL,next_attempt_at=clock_timestamp()+INTERVAL '20 seconds',last_error='' WHERE operation_id=$1`,op.GetOperationId())
 if err!=nil{return nil,status.Errorf(codes.Internal,"schedule GPU wait delivery: %v",err)}
 if tag.RowsAffected()!=1{return nil,status.Error(codes.FailedPrecondition,"durable outbox event required")}
 _,err=tx.Exec(ctx,`UPDATE operations SET status='pending',phase='waiting_resources',gpu_wait_count=gpu_wait_count+1,gpu_wait_token=$2::uuid,gpu_wait_until=clock_timestamp()+INTERVAL '20 seconds',dispatch_token=NULL,dispatch_owner='',dispatch_lease_until=NULL,error_code='GPU_BUSY',error_message=$3,retryable=FALSE,updated_at=NOW(),reconcile_checked_at=NULL,reconcile_note='' WHERE operation_id=$1`,op.GetOperationId(),r.GetDispatchToken(),reason)
 if err!=nil{return nil,status.Errorf(codes.Internal,"record GPU wait: %v",err)}
 // Use the same due time; a premature duplicate may be committed only because
 // the durable future publication is guaranteed to occur after the wait gate.
 if _,err=tx.Exec(ctx,`UPDATE operation_outbox SET next_attempt_at=(SELECT gpu_wait_until FROM operations WHERE operation_id=$1) WHERE operation_id=$1`,op.GetOperationId());err!=nil{return nil,status.Errorf(codes.Internal,"align GPU wait delivery: %v",err)}
 return commitOperation(ctx,tx,op.GetOperationId())
}
