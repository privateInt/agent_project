// Package node is the SSH-invoked remote worker. It accepts operation identities,
// never caller-supplied images, environment variables, or shell commands.
package node

import (
 "context"
 "errors"
 "fmt"
 "reflect"
 "time"
 "github.com/google/uuid"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
type Operations interface { Get(context.Context,string)(*operationv1.Operation,error) }
type Profiles interface { Get(string)(execution.Profile,error) }
type Runner interface { Run(context.Context,execution.Command,execution.Profile)error }
type Agent struct {
 Name string
 Operations Operations
 Profiles Profiles
 Runner Runner
 Identity func(context.Context)(string,string,error)
 Check func(context.Context,execution.Profile)error
 Inspect func(context.Context,*operationv1.Operation)(*remotenode.Evidence,error)
 Now func()time.Time
}
func validUUID(v string)bool{u,e:=uuid.Parse(v);return e==nil&&u!=uuid.Nil&&u.String()==v}
func(a Agent)Handle(ctx context.Context,r remotenode.Request)remotenode.Response{
 out:=remotenode.Response{Version:1,Action:r.Action,NodeName:a.Name,OperationID:r.OperationID,Status:"unknown"}
 reject:=func(message string)remotenode.Response{out.Status="rejected";out.Message=message;return out}
 daemon,arch,err:=a.Identity(ctx);if err!=nil {out.Message="Docker identity unavailable";return out}
 out.DaemonID=daemon;out.Architecture=arch
 if r.Version!=1||r.NodeName!=a.Name||!remotenode.ValidName(a.Name)||arch!="amd64" {return reject("node identity or protocol mismatch")}
 if r.DaemonID!=""&&r.DaemonID!=daemon{return reject("Docker daemon identity mismatch")}
 if r.Action=="probe" {
  // Opening a forwarded TCP socket alone does not prove that its destination
  // service is responding. A read-only missing UUID probes the real RPC.
  probeCtx,cancel:=context.WithTimeout(ctx,3*time.Second)
  _,lookupErr:=a.Operations.Get(probeCtx,uuid.NewString());cancel()
  if lookupErr!=nil&&status.Code(lookupErr)!=codes.NotFound{out.Message="Operation RPC tunnel unavailable";return out}
  p,err:=a.Profiles.Get("training.run");if err!=nil{return reject("training profile unavailable")}
  if err=a.Check(ctx,p);err!=nil{out.Message=err.Error();return out}
  out.Status="ready";return out
 }
 if (r.Action!="launch"&&r.Action!="inspect")||!validUUID(r.OperationID)||r.DaemonID==""{return reject("invalid action or operation identity")}
 op,err:=a.Operations.Get(ctx,r.OperationID)
 if err!=nil{out.Message="Operation lookup unavailable";return out}
 if op==nil||op.GetOperationId()!=r.OperationID||op.GetExecutionType()!="training.run"||op.GetResourceType()!="training_run"||!validUUID(op.GetResourceId())||op.GetResources().GetNodeName()!=a.Name||op.GetDispatchProtocol()!=1||!validUUID(op.GetDispatchToken()) {return reject("Operation placement or execution contract mismatch")}
 resources,err:=execution.NormalizeResources(op.GetResources(),0)
 if err!=nil||resources.GPUCount!=1||len(resources.GPUIds)!=1{return reject("remote training requires one exact GPU ID")}
 if r.Action=="inspect" {
  evidence,err:=a.Inspect(ctx,op);if err!=nil{out.Message="container evidence unavailable or mismatched";return out}
  out.Status="observed";out.Evidence=evidence;return out
 }
 // A stale request must NEVER be converted to busy/rejected after another
 // generation has begun: the dispatcher must retain it for reconciliation.
 if r.DispatchToken!=op.GetDispatchToken(){out.Message="dispatch generation changed";return out}
 now:=time.Now();if a.Now!=nil{now=a.Now()}
 if op.GetStatus()!="dispatching"||op.GetExecutionStartedAt()!=nil||op.GetDispatchLeaseUntil()==nil||!op.GetDispatchLeaseUntil().AsTime().After(now) {
  evidence,err:=a.Inspect(ctx,op)
  if op.GetExecutionStartedAt()!=nil&&err==nil&&evidence!=nil&&evidence.State!="missing" {out.Status="accepted";out.Evidence=evidence;return out}
  out.Message="execution authorization no longer launchable";return out
 }
 // Replayed RPC with the same grant does not invoke Docker a second time.
 evidence,err:=a.Inspect(ctx,op)
 if err!=nil{out.Message="pre-launch container inspection unavailable";return out}
 if evidence!=nil&&evidence.State!="missing"{out.Status="accepted";out.Evidence=evidence;return out}
 p,err:=a.Profiles.Get("training.run");if err!=nil{return reject("training profile unavailable")}
 if err=a.Check(ctx,p);err!=nil{out.Message=err.Error();return out}
 cmd:=execution.Command{OperationID:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),Kind:op.GetKind(),ExecutionType:op.GetExecutionType(),ResourceType:op.GetResourceType(),ResourceID:op.GetResourceId(),Resources:resources}
 err=a.Runner.Run(ctx,cmd,p)
 if err==nil {out.Status="accepted";return out}
 var busy *gpuadmission.BusyError
 if errors.As(err,&busy){out.Status="busy";out.Message=busy.Owner;return out}
 // Docker output can contain job environment. Return a fixed diagnostic; a
 // lost reply or an uncertain create keeps the node's durable admission claim.
 out.Message="Docker launch unconfirmed; retain operation and GPU claim";return out
}

func ValidateContainer(op *operationv1.Operation,id,image string,labels map[string]string,state string,exitCode int,ids []int32)(*remotenode.Evidence,error){
 if id==""||image==""||labels["model-platform.owner"]!="operation-dispatcher"||labels["model-platform.operation_id"]!=op.GetOperationId()||labels["model-platform.dispatch_token"]!=op.GetDispatchToken()||labels["model-platform.execution_type"]!="training.run"||labels["model-platform.execution_node"]!=op.GetResources().GetNodeName()||!reflect.DeepEqual(gpuadmission.SortedIDs(ids),gpuadmission.SortedIDs(op.GetResources().GetGpuIds())) {return nil,fmt.Errorf("remote container identity/resources mismatch")}
 return &remotenode.Evidence{ContainerID:id,ImageID:image,State:state,ExitCode:exitCode,GPUIds:ids},nil
}
