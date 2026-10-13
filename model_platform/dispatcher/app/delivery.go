package app

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "log"
 "time"
 "github.com/google/uuid"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
)

type Operations interface {
 Claim(context.Context,string)(*operationv1.ClaimOperationResponse,error)
 Fail(context.Context,string,string,string,string,string,bool)error
}
type Profiles interface { Get(string)(execution.Profile,error) }
type Runner interface { Run(context.Context,execution.Command,execution.Profile)error }

type Delivery struct { Operations Operations; Profiles Profiles; Runner Runner }

// Handle returns nil only when this record may be committed. Any ambiguous
// outcome must leave the offset uncommitted. Reconciliation is a separate step.
func(d Delivery) Handle(ctx context.Context,payload []byte)error {
 var cmd execution.Command
 if err:=json.Unmarshal(payload,&cmd);err!=nil{return fmt.Errorf("invalid command (retained): %w",err)}
 if _,err:=uuid.Parse(cmd.OperationID);err!=nil{return fmt.Errorf("invalid operation ID (retained): %w",err)}
 claimCtx,cancel:=context.WithTimeout(ctx,5*time.Second)
 claim,err:=d.Operations.Claim(claimCtx,cmd.OperationID);cancel()
 if err!=nil{return fmt.Errorf("claim uncertain operation=%s: %w",cmd.OperationID,err)}
 op:=claim.GetOperation()
 if op==nil || op.GetOperationId()!=cmd.OperationID {return fmt.Errorf("claim identity mismatch operation=%s",cmd.OperationID)}
 if !claim.GetClaimed() {
  if op.GetStatus()=="pending" && op.GetPhase()=="waiting_resources" && op.GetGpuWaitUntil()!=nil {return nil}
  switch op.GetStatus() {
  case "running","succeeded","failed","cancelling","cancelled":return nil
  default:return fmt.Errorf("operation=%s state=%s requires reconciliation; offset retained",cmd.OperationID,op.GetStatus())
  }
 }
 if op.GetStatus()!="dispatching" {return fmt.Errorf("unexpected claimed state %s",op.GetStatus())}
 // All routing and placement come from PostgreSQL, not the Kafka payload.
 if op.GetDispatchProtocol()!=1 || op.GetDispatchToken()=="" {return fmt.Errorf("execution gate contract missing")}
 cmd.DispatchToken=op.GetDispatchToken()
 cmd.Kind=op.GetKind();cmd.ExecutionType=op.GetExecutionType()
 cmd.ResourceType=op.GetResourceType();cmd.ResourceID=op.GetResourceId()
 cmd.Resources=execution.ResourcesFromProto(op.GetResources())
 profile,err:=d.Profiles.Get(cmd.ExecutionType)
 if err!=nil {
  failCtx,stop:=context.WithTimeout(ctx,5*time.Second);defer stop()
  if failErr:=d.Operations.Fail(failCtx,cmd.OperationID,cmd.DispatchToken,"dispatch_failed","UNSUPPORTED_EXECUTION_TYPE",err.Error(),false);failErr!=nil {
   return fmt.Errorf("failure report uncertain operation=%s: %w",cmd.OperationID,failErr)
  }
  return nil
 }
 launchCtx,stop:=context.WithTimeout(ctx,30*time.Second);defer stop()
 if err=d.Runner.Run(launchCtx,cmd,profile);err!=nil {
  var cancelled *gpuadmission.CancelledError
  if errors.As(err,&cancelled) {return nil} // Durable host tombstone owns cleanup.
  var rejected *remotenode.RejectedError
  if errors.As(err,&rejected) {
   reportCtx,cancel:=context.WithTimeout(ctx,5*time.Second);defer cancel()
   if e:=d.Operations.Fail(reportCtx,cmd.OperationID,cmd.DispatchToken,"dispatch_failed","NODE_PLACEMENT_REJECTED",rejected.Error(),false);e!=nil{return fmt.Errorf("node rejection report uncertain: %w",e)}
   return nil
  }
  var busy *gpuadmission.BusyError
  if errors.As(err,&busy) {
   deferred,ok:=d.Operations.(interface{DeferGPU(context.Context,string,string,string)error})
   if !ok{return fmt.Errorf("GPU wait reporting contract unavailable; record retained")}
   reportCtx,cancel:=context.WithTimeout(ctx,10*time.Second);defer cancel()
   if reportErr:=deferred.DeferGPU(reportCtx,cmd.OperationID,cmd.DispatchToken,busy.Error());reportErr!=nil{return fmt.Errorf("GPU wait report uncertain operation=%s: %w",cmd.OperationID,reportErr)}
   log.Printf("GPU wait scheduled operation=%s: %s",cmd.OperationID,busy.Error())
   // The atomic wait/outbox commit now owns the next delivery, so the current
   // record can be acknowledged without restarting this consumer.
   return nil
  }
  // A docker CLI error/timeout is not proof that no container was created.
  // Preserve dispatching instead of changing a potentially running job to failed.
  return fmt.Errorf("launch uncertain operation=%s; reconciliation required: %w",cmd.OperationID,err)
 }
 return nil
}

// Commit retry never repeats Handle or launches another container.
func Commit(ctx context.Context,call func(context.Context)error)error {
 var err error
 for attempt:=1;attempt<=3;attempt++ {
  callCtx,cancel:=context.WithTimeout(ctx,5*time.Second)
  err=call(callCtx);cancel()
  if err==nil{return nil}
  if ctx.Err()!=nil{return ctx.Err()}
 }
 return fmt.Errorf("offset commit failed after 3 attempts: %w",err)
}
