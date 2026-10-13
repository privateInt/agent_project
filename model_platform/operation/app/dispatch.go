package app

import (
 "context"
 "strings"

 "github.com/google/uuid"
 "github.com/jackc/pgx/v5"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

// The dispatch lease covers process launch, not GPU execution. It is never
// renewed or reassigned after BeginExecution has granted execution ownership.
const maxDispatchGenerations int32 = 3

func validID(id string)bool{_,err:=uuid.Parse(id);return err==nil}
func leaseAlive(ctx context.Context,tx pgx.Tx,id string)(bool,error){
 var alive bool
 // Read the wall clock after acquiring the row lock. Transaction NOW() can
 // predate a long lock wait and would incorrectly accept an expired lease.
 err:=tx.QueryRow(ctx,`SELECT COALESCE(dispatch_lease_until>clock_timestamp(),FALSE) FROM operations WHERE operation_id=$1`,id).Scan(&alive)
 if err!=nil{return false,status.Errorf(codes.Internal,"read lease: %v",err)}
 return alive,nil
}

func(s *Service)ClaimOperation(ctx context.Context,r *operationv1.ClaimOperationRequest)(*operationv1.ClaimOperationResponse,error){
 id:=strings.TrimSpace(r.GetOperationId())
 owner:=strings.TrimSpace(r.GetOwnerId())
 if !validID(id)||owner==""||len(owner)>128{return nil,status.Error(codes.InvalidArgument,"valid operation_id and owner_id required")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin claim: %v",err)};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,id);if err!=nil{return nil,err}
 if op.GetStatus()!="pending"{return &operationv1.ClaimOperationResponse{Claimed:false,Operation:op},nil}
 var waiting bool
 if err:=tx.QueryRow(ctx,`SELECT COALESCE(gpu_wait_until>clock_timestamp(),FALSE) FROM operations WHERE operation_id=$1`,id).Scan(&waiting);err!=nil{return nil,status.Errorf(codes.Internal,"read GPU wait deadline: %v",err)}
 if waiting{return &operationv1.ClaimOperationResponse{Claimed:false,Operation:op},nil}
 if op.GetDispatchProtocol()!=1{return nil,status.Error(codes.FailedPrecondition,"legacy pending operation requires explicit migration; launch denied")}
 if op.GetExecutionStartedAt()!=nil||op.GetDispatchGeneration()-op.GetGpuWaitCount()>=maxDispatchGenerations{return nil,status.Error(codes.FailedPrecondition,"execution already started or dispatch budget exhausted")}
 token:=uuid.NewString()
 _,err=tx.Exec(ctx,`UPDATE operations SET status='dispatching',phase='dispatching',attempt=GREATEST(attempt,1),
 started_at=COALESCE(started_at,NOW()),dispatch_generation=dispatch_generation+1,dispatch_token=$2::uuid,
 dispatch_owner=$3,dispatch_lease_until=clock_timestamp()+INTERVAL '120 seconds',gpu_wait_until=NULL,error_code='',error_message='',updated_at=NOW(),reconcile_checked_at=NULL,reconcile_note=''
 WHERE operation_id=$1`,id,token,owner)
 if err!=nil{return nil,status.Errorf(codes.Internal,"claim operation: %v",err)}
 op,err=commitOperation(ctx,tx,id);if err!=nil{return nil,err}
 return &operationv1.ClaimOperationResponse{Claimed:true,Operation:op},nil
}

// A process UUID stays constant across transport retries, but changes whenever
// the executable starts again. Thus Docker restart cannot run a job twice.
func(s *Service)BeginExecution(ctx context.Context,r *operationv1.BeginExecutionRequest)(*operationv1.Operation,error){
 if !validID(r.GetOperationId())||!validID(r.GetDispatchToken())||!validID(r.GetExecutionInstanceId()){
  return nil,status.Error(codes.InvalidArgument,"operation, dispatch token and process UUID required")
 }
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin execution gate: %v",err)};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,r.GetOperationId());if err!=nil{return nil,err}
 if op.GetDispatchProtocol()!=1||op.GetDispatchToken()!=r.GetDispatchToken()||op.GetResourceId()!=r.GetResourceId()||op.GetExecutionType()!=r.GetExecutionType(){
  return nil,status.Error(codes.FailedPrecondition,"stale execution token or input identity mismatch")
 }
 if op.GetExecutionStartedAt()!=nil{
  if op.GetExecutionInstanceId()==r.GetExecutionInstanceId()&&op.GetStatus()=="running"{return op,nil}
  return nil,status.Error(codes.FailedPrecondition,"execution already granted to a process or operation is terminal")
 }
 alive,err:=leaseAlive(ctx,tx,op.GetOperationId());if err!=nil{return nil,err}
 if op.GetStatus()!="dispatching"||!alive{return nil,status.Error(codes.FailedPrecondition,"dispatch lease expired or operation is not dispatching")}
 _,err=tx.Exec(ctx,`UPDATE operations SET execution_instance_id=$2::uuid,execution_started_at=NOW(),
 status='running',phase='execution_started',updated_at=NOW(),reconcile_checked_at=NULL WHERE operation_id=$1`,op.GetOperationId(),r.GetExecutionInstanceId())
 if err!=nil{return nil,status.Errorf(codes.Internal,"grant execution: %v",err)}
 return commitOperation(ctx,tx,op.GetOperationId())
}

// Revocation and Outbox re-arming are one transaction. Kafka carries no token;
// the next Claim receives a new one. The old process can no longer Begin.
func(s *Service)RecoverDispatch(ctx context.Context,r *operationv1.RecoverDispatchRequest)(*operationv1.Operation,error){
 if !validID(r.GetOperationId())||!validID(r.GetExpectedDispatchToken()){return nil,status.Error(codes.InvalidArgument,"operation and expected token required")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin dispatch recovery: %v",err)};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,r.GetOperationId());if err!=nil{return nil,err}
 if op.GetDispatchProtocol()!=1||op.GetStatus()!="dispatching"||op.GetDispatchToken()!=r.GetExpectedDispatchToken()||op.GetExecutionStartedAt()!=nil||op.GetDispatchGeneration()-op.GetGpuWaitCount()>=maxDispatchGenerations{
  return nil,status.Error(codes.FailedPrecondition,"dispatch changed, execution started, or recovery budget exhausted")
 }
 alive,err:=leaseAlive(ctx,tx,op.GetOperationId());if err!=nil{return nil,err}
 if alive{return nil,status.Error(codes.FailedPrecondition,"dispatch lease is still valid")}
 tag,err:=tx.Exec(ctx,`UPDATE operation_outbox SET published_at=NULL,next_attempt_at=NOW(),last_error='' WHERE operation_id=$1`,op.GetOperationId())
 if err!=nil{return nil,status.Errorf(codes.Internal,"rearm dispatch event: %v",err)}
 if tag.RowsAffected()!=1{return nil,status.Error(codes.FailedPrecondition,"no durable outbox event; recovery denied")}
 _,err=tx.Exec(ctx,`UPDATE operations SET status='pending',phase='launch_retry_queued',dispatch_token=NULL,dispatch_owner='',dispatch_lease_until=NULL,
 updated_at=NOW(),reconcile_checked_at=NOW(),reconcile_note='expired unstarted dispatch revoked and event rearmed' WHERE operation_id=$1`,op.GetOperationId())
 if err!=nil{return nil,status.Errorf(codes.Internal,"requeue unstarted dispatch: %v",err)}
 return commitOperation(ctx,tx,op.GetOperationId())
}

// Dispatcher failures need a current token too; an old dispatcher must not
// fail a newer generation or a process that has already started execution.
func(s *Service)FailDispatch(ctx context.Context,r *operationv1.FailDispatchRequest)(*operationv1.Operation,error){
 if !validID(r.GetOperationId())||!validID(r.GetDispatchToken())||r.GetErrorCode()==""||len(r.GetErrorCode())>128{return nil,status.Error(codes.InvalidArgument,"valid operation, token and error code required")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin dispatch failure: %v",err)};defer tx.Rollback(ctx)
 op,err:=lockOperation(ctx,tx,r.GetOperationId());if err!=nil{return nil,err}
 alive,err:=leaseAlive(ctx,tx,op.GetOperationId());if err!=nil{return nil,err}
 if op.GetDispatchProtocol()!=1||op.GetStatus()!="dispatching"||op.GetDispatchToken()!=r.GetDispatchToken()||op.GetExecutionStartedAt()!=nil||!alive{return nil,status.Error(codes.FailedPrecondition,"dispatch no longer owned")}
 _,err=tx.Exec(ctx,`UPDATE operations SET status='failed',phase='dispatch_failed',error_code=$2,error_message=$3,retryable=FALSE,completed_at=NOW(),updated_at=NOW() WHERE operation_id=$1`,op.GetOperationId(),r.GetErrorCode(),r.GetErrorMessage())
 if err!=nil{return nil,status.Errorf(codes.Internal,"save dispatch failure: %v",err)}
 return commitOperation(ctx,tx,op.GetOperationId())
}
