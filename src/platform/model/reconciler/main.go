// Reconciler repairs status from evidence and requeues expired pre-start leases.
// Docker launch remains the dispatcher's responsibility.
package main
import (
 "context"
 "log"
 "os"
 "os/signal"
 "syscall"
 "time"
 operationv1 "model-platform/gen/operation/v1"
 managementv1 "model-platform/gen/management/v1"
 trainingv1 "model-platform/gen/training/v1"
 deploymentv1 "model-platform/gen/deployment/v1"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/grpc/credentials/insecure"
)
type worker struct {
 ops operationv1.OperationServiceClient
 management managementv1.ManagementServiceClient
 training trainingv1.TrainingServiceClient
 deployment deploymentv1.DeploymentServiceClient
 node string
}
func main(){if err:=run();err!=nil{log.Fatal(err)}}
func run()error{
 ctx,stop:=signal.NotifyContext(context.Background(),syscall.SIGINT,syscall.SIGTERM);defer stop()
 connect:=func(key,fallback string)(*grpc.ClientConn,error){return grpc.NewClient(env(key,fallback),grpc.WithTransportCredentials(insecure.NewCredentials()))}
 oc,err:=connect("OPERATION_GRPC_ADDRESS","operation:9000");if err!=nil{return err};defer oc.Close()
 mc,err:=connect("MANAGEMENT_GRPC_ADDRESS","management:9000");if err!=nil{return err};defer mc.Close()
 tc,err:=connect("TRAINING_GRPC_ADDRESS","training:9000");if err!=nil{return err};defer tc.Close()
 dc,err:=connect("DEPLOYMENT_GRPC_ADDRESS","deployment:9000");if err!=nil{return err};defer dc.Close()
 w:=worker{operationv1.NewOperationServiceClient(oc),managementv1.NewManagementServiceClient(mc),trainingv1.NewTrainingServiceClient(tc),deploymentv1.NewDeploymentServiceClient(dc),os.Getenv("DISPATCHER_NODE_NAME")}
 log.Print("operation reconciler ready (pre-start lease recovery enabled; no restart after execution grant)")
 for ctx.Err()==nil{
  w.cycle(ctx)
  select{case <-ctx.Done():return nil;case <-time.After(15*time.Second):}
 }
 return nil
}
func(w worker)cycle(ctx context.Context){
 c,cancel:=context.WithTimeout(ctx,5*time.Second)
 candidates,err:=w.ops.ListReconcileOperations(c,&operationv1.ListReconcileOperationsRequest{Limit:50});cancel()
 if err!=nil{log.Printf("reconciler list: %v",err);return}
 for _,op:=range candidates.GetOperations(){
  if ctx.Err()!=nil{return}
  c,cancel:=context.WithTimeout(ctx,15*time.Second)
  if op.GetDispatchProtocol()==1 && op.GetStatus()=="dispatching" && op.GetExecutionStartedAt()==nil && op.GetDispatchLeaseUntil()!=nil && !op.GetDispatchLeaseUntil().AsTime().After(time.Now()) {
   recovered,recoverErr:=w.ops.RecoverDispatch(c,&operationv1.RecoverDispatchRequest{OperationId:op.GetOperationId(),ExpectedDispatchToken:op.GetDispatchToken()})
   if recoverErr==nil {log.Printf("expired dispatch requeued operation=%s generation=%d status=%s",op.GetOperationId(),op.GetDispatchGeneration(),recovered.GetStatus());cancel();continue}
   if status.Code(recoverErr)!=codes.FailedPrecondition {log.Printf("dispatch recovery operation=%s: %v",op.GetOperationId(),recoverErr);cancel();continue}
  }
  target,note:=w.observe(c,op)
  _,err:=w.ops.ApplyReconciliation(c,&operationv1.ApplyReconciliationRequest{OperationId:op.GetOperationId(),ExpectedStatus:op.GetStatus(),ExpectedAttempt:op.GetAttempt(),ExpectedUpdatedAt:op.GetUpdatedAt(),TargetStatus:target,Note:note})
  cancel()
  if status.Code(err)==codes.FailedPrecondition {log.Printf("reconciler skipped changed operation=%s",op.GetOperationId());continue}
  if err!=nil{log.Printf("reconciler apply operation=%s: %v",op.GetOperationId(),err);continue}
  log.Printf("reconciler operation=%s target=%q note=%s",op.GetOperationId(),target,note)
 }
}
func env(key,fallback string)string{if v:=os.Getenv(key);v!=""{return v};return fallback}
