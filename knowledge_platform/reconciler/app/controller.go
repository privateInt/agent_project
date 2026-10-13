package app
import(
 "context";"encoding/json";"fmt";"log";"time"
 "knowledge-platform/dispatcher/executor"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 "knowledge-platform/internal/indexing"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/chunking";"knowledge-platform/internal/execution"
)
type Observer interface{Inspect(context.Context,string,string,string)(*executor.Runtime,error)}
type Controller struct{Operations operationv1.OperationServiceClient;Knowledge chunkingv1.ChunkingServiceClient;Docker Observer;Indexes indexingv1.IndexingServiceClient}
func(c Controller)Run(ctx context.Context){ticker:=time.NewTicker(15*time.Second);defer ticker.Stop();for{c.Tick(ctx);select{case <-ctx.Done():return;case <-ticker.C:}}}
func(c Controller)Tick(ctx context.Context){
 q,cancel:=context.WithTimeout(ctx,10*time.Second);list,e:=c.Operations.ListReconcileOperations(q,&operationv1.ListReconcileOperationsRequest{Limit:50});cancel();if e!=nil{log.Print("knowledge reconciliation list unavailable");return}
 for _,op:=range list.Operations{q,cancel:=context.WithTimeout(ctx,20*time.Second);e=c.Reconcile(q,op);cancel();if e!=nil{log.Printf("reconciliation operation=%s unavailable",op.OperationId)}}
}
func(c Controller)Reconcile(ctx context.Context,op *operationv1.Operation)error{
 note:=func(target,message string)error{_,e:=c.Operations.ApplyReconciliation(ctx,&operationv1.ApplyReconciliationRequest{OperationId:op.OperationId,ExpectedStatus:op.Status,ExpectedAttempt:op.Attempt,ExpectedUpdatedAt:op.UpdatedAt,TargetStatus:target,Note:message});return e}
 if !execution.Supported(op.GetExecutionType(),op.GetResourceType())||op.GetDispatchProtocol()!=1{return note("","manual review: unsupported execution contract")}
 if op.GetExecutionStartedAt()==nil {
  if op.GetStatus()!="dispatching"||op.GetDispatchLeaseUntil()==nil||time.Now().Before(op.DispatchLeaseUntil.AsTime()){return note("","waiting for current launch lease")}
  if op.GetDispatchGeneration()>=3{return note("","manual review: pre-start recovery budget exhausted")}
  _,e:=c.Operations.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:op.OperationId,ExpectedDispatchToken:op.DispatchToken});return e
 }
 if op.GetExecutionType()=="indexing.build" {
  if c.Indexes==nil{return fmt.Errorf("index observation unavailable")}
  raw,e:=c.Indexes.GetVersion(ctx,&indexingv1.VersionRequest{IndexVersionId:op.ResourceId});if e!=nil{return fmt.Errorf("index observation unavailable")}
  var v indexing.Version;if json.Unmarshal(raw.Json,&v)!=nil||v.ID!=op.ResourceId||v.OperationID!=op.OperationId{return note("","manual review: index identity mismatch")}
  if v.State=="ready"&&v.Count>0&&v.Next==v.Count&&v.ReferenceState=="held"{return note("succeeded","immutable index and Data references were verified")}
  if v.State=="deleted"||v.State=="failed"{return note("failed","index build was deleted or failed; no automatic re-execution")}
  runtime,e:=c.Docker.Inspect(ctx,op.OperationId,op.DispatchToken,op.ResourceId);if e!=nil{return note("","manual review: index runtime observation unavailable")}
  if runtime.State.Running{return note("","authorized index process remains running")}
  return note("","manual review: exited index process without final receipt; delete or create a new version, no automatic inference replay")
 }
 run,e:=c.Knowledge.GetRun(ctx,&chunkingv1.GetRunRequest{ChunkingRunId:op.ResourceId});if e!=nil{return fmt.Errorf("domain observation unavailable")}
 if run.GetOperationId()!=op.OperationId||run.GetChunkingRunId()!=op.ResourceId{return note("","manual review: domain identity mismatch")}
 if run.ResultSha256!="" {
  if !chunking.ValidDigest(run.ResultSha256)||run.ResultSizeBytes<1||run.ResultSizeBytes>chunking.MaxOutputBytes||run.ChunkCount<1{return note("","manual review: result receipt mismatch")}
  return note("succeeded","immutable chunk result was verified and registered by the authorized process")
 }
 runtime,e:=c.Docker.Inspect(ctx,op.OperationId,op.DispatchToken,op.ResourceId);if e!=nil{return note("","manual review: runtime observation unavailable; no automatic re-execution")}
 if runtime.State.Running{return note("","authorized CPU process remains running")}
 return note("","manual review: exited process without confirmed result; no automatic re-execution")
}
