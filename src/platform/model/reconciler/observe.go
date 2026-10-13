package main
import (
 "context"
 "encoding/json"
 "fmt"
 "os/exec"
 "strings"
 "time"
 "model-platform/internal/remotenode"
 operationv1 "model-platform/gen/operation/v1"
 managementv1 "model-platform/gen/management/v1"
 trainingv1 "model-platform/gen/training/v1"
 deploymentv1 "model-platform/gen/deployment/v1"
)

type containerEvidence struct{State struct{Status string;Running bool;Paused bool;Restarting bool};Labels map[string]string}

func decision(domain,container string)(string,string){
 switch domain{
 case "succeeded":return "succeeded","domain completion confirmed"
 case "failed":return "failed","domain failure confirmed"
 }
 if container=="running"{return "running","matching Docker container is running"}
 return "","manual review required: no definitive completion or live-container evidence ("+container+")"
}
func(w worker)observe(ctx context.Context,op *operationv1.Operation)(string,string){
 if op.GetDispatchProtocol()==1 && op.GetExecutionStartedAt()==nil {
  if op.GetDispatchGeneration()-op.GetGpuWaitCount()>=3 {return "","manual review required: pre-start recovery budget exhausted"}
  return "","execution authorization pending; waiting for lease recovery"
 }
 domain,err:=w.domain(ctx,op)
 if err!=nil{domain="unknown"}
 if domain=="succeeded"||domain=="failed" {return decision(domain,"")}
 node:=op.GetResources().GetNodeName()
 if node!=""&&node!=w.node {
  nodes,e:=remotenode.Load();if e!=nil{return "","remote node configuration unavailable"}
  n,ok:=nodes.Find(node);if !ok||op.GetExecutionType()!="training.run" {return "","manual review required: unsupported remote placement"}
  // Disabled nodes remain observable; disabling admission cannot erase a Job.
  response,e:=remotenode.NewClient().Call(ctx,n,remotenode.Request{Action:"inspect",OperationID:op.GetOperationId()})
  if e!=nil||response.Status!="observed"||response.Evidence==nil{return "","remote container evidence unavailable; no restart or relocation"}
  return decision(domain,response.Evidence.State)
 }
 state,inspectErr:=inspectContainer(ctx,op)
 if inspectErr!=nil{return "","manual review required: Docker inspect unavailable or identity mismatch"}
 target,note:=decision(domain,state)
 if err!=nil {note+="; domain lookup unavailable"}
 return target,note
}
func inspectContainer(parent context.Context,op *operationv1.Operation)(string,error){
 ctx,cancel:=context.WithTimeout(parent,5*time.Second);defer cancel()
 // Do not return Config.Env: inspect only state and ownership labels.
 name:="model-op-"+op.GetOperationId()
 if op.GetDispatchProtocol()==1 {name+="-"+op.GetDispatchToken()}
 output,err:=exec.CommandContext(ctx,"docker","inspect","--type","container","--format",`{"State":{{json .State}},"Labels":{{json .Config.Labels}}}`,name).CombinedOutput()
 if err!=nil{
  if strings.Contains(string(output),"No such object")||strings.Contains(string(output),"No such container"){return "missing",nil}
  return "",fmt.Errorf("inspect failed: %w",err)
 }
 var evidence containerEvidence
 if err=json.Unmarshal(output,&evidence);err!=nil{return "",err}
 return validateEvidence(op,evidence)
}
func validateEvidence(op *operationv1.Operation,e containerEvidence)(string,error){
 if op.GetDispatchProtocol()==1 && (op.GetExecutionStartedAt()==nil || e.Labels["model-platform.dispatch_token"]!=op.GetDispatchToken()) {return "",fmt.Errorf("unapproved process or generation mismatch")}
 if e.Labels["model-platform.owner"]!="operation-dispatcher"||e.Labels["model-platform.operation_id"]!=op.GetOperationId()||e.Labels["model-platform.execution_type"]!=op.GetExecutionType(){return "",fmt.Errorf("container identity mismatch")}
 if e.State.Running&&!e.State.Paused&&!e.State.Restarting&&e.State.Status=="running"{return "running",nil}
 return e.State.Status,nil
}
func(w worker)domain(parent context.Context,op *operationv1.Operation)(string,error){
 ctx,cancel:=context.WithTimeout(parent,5*time.Second);defer cancel()
 switch op.GetExecutionType(){
 case "training.run":
  if op.GetResourceType()!="training_run"{return "",fmt.Errorf("resource mismatch")}
  r,e:=w.training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:op.GetResourceId()});if e!=nil{return "",e}
  if r.GetTrainingRunId()!=op.GetResourceId(){return "",fmt.Errorf("training identity mismatch")}
  if r.GetStatus()=="succeeded"&&r.GetOutputModelVersionId()=="" {return "unknown",nil}
  return r.GetStatus(),nil
 case "management.import-huggingface":
  if op.GetResourceType()!="model_version"{return "",fmt.Errorf("resource mismatch")}
  r,e:=w.management.GetModelVersion(ctx,&managementv1.GetModelVersionRequest{ModelVersionId:op.GetResourceId()});if e!=nil{return "",e}
  if r.GetModelVersionId()!=op.GetResourceId(){return "",fmt.Errorf("model identity mismatch")}
  if r.GetState()=="ready"&&r.GetArtifactUri()!=""&&r.GetArtifactSha256()!="" {return "succeeded",nil}
  if r.GetState()=="failed" {return "failed",nil};return "unknown",nil
 case "deployment.apply", "deployment.serve":
  if op.GetResourceType()!="deployment"{return "",fmt.Errorf("resource mismatch")}
  r,e:=w.deployment.GetDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:op.GetResourceId()});if e!=nil{return "",e}
  if r.GetDeploymentId()!=op.GetResourceId(){return "",fmt.Errorf("deployment identity mismatch")}
  if op.GetExecutionType()=="deployment.serve" {
   if r.GetOperationId()!=op.GetOperationId(){return "",fmt.Errorf("serving operation mismatch")}
   if r.GetApplyStatus()=="ready"{return "succeeded",nil}
   if r.GetApplyStatus()=="failed"{return "failed",nil}
   return "unknown",nil
  }
  if r.GetStatus()=="ready" {return "succeeded",nil}
  if r.GetStatus()=="failed" {return "failed",nil};return "unknown",nil
 default:return "",fmt.Errorf("unsupported execution type")
 }
}
