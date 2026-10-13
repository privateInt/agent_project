package main

import (
 "context"
 "encoding/json"
 "flag"
 "fmt"
 "os"
 "strings"
 "time"
 dispatchapp "model-platform/dispatcher/app"
 "model-platform/internal/gpuadmission"
 "github.com/google/uuid"
)

// Explicit recovery for the previous release's confirmed GPU_BUSY incident.
// This is not a general retry or a reset of the launch failure budget.
func recoverGPUWait(args []string)error{
 flags:=flag.NewFlagSet("recover-gpu-wait",flag.ContinueOnError)
 id:=flags.String("recover-gpu-wait","","Operation UUID whose old dispatch waited for GPUs")
 apply:=flags.Bool("apply",false,"requeue after proving no Job/creation intent exists")
 if err:=flags.Parse(args);err!=nil{return err}
 parsed,err:=uuid.Parse(*id);if err!=nil||parsed==uuid.Nil||flags.NArg()!=0{return fmt.Errorf("valid --recover-gpu-wait UUID required")}
 ctx,cancel:=context.WithTimeout(context.Background(),45*time.Second);defer cancel()
 ops,err:=dispatchapp.ConnectOperation(env("OPERATION_GRPC_ADDRESS","operation:9000"));if err!=nil{return err};defer ops.Close()
 op,err:=ops.Get(ctx,parsed.String());if err!=nil{return err}
 // Re-running a successful recovery never resets its waiting deadline.
 if op.GetStatus()=="pending" && op.GetPhase()=="waiting_resources" {return json.NewEncoder(os.Stdout).Encode(map[string]any{"operation_id":op.GetOperationId(),"status":op.GetStatus(),"phase":op.GetPhase(),"already_deferred":true})}
 if op.GetDispatchProtocol()!=1||op.GetStatus()!="dispatching"||op.GetPhase()!="dispatching"||op.GetExecutionStartedAt()!=nil||op.GetDispatchToken()==""||op.GetResources().GetGpuCount()<1||op.GetDispatchLeaseUntil()==nil||op.GetDispatchLeaseUntil().AsTime().After(time.Now()){return fmt.Errorf("recovery requires an expired, unstarted GPU dispatch")}
 node:=strings.TrimSpace(op.GetResources().GetNodeName())
 if node!="" && node!=env("DISPATCHER_NODE_NAME",""){return fmt.Errorf("recovery must inspect the requested Docker host")}
 return gpuadmission.FromEnvironment().WithUnstartedProof(ctx,op.GetOperationId(),func()error{
  result:=map[string]any{"operation_id":op.GetOperationId(),"dispatch_generation":op.GetDispatchGeneration(),"gpu_wait_count_before":op.GetGpuWaitCount(),"gpu_ids":op.GetResources().GetGpuIds(),"no_job_or_creation_intent":true,"applied":false}
  if *apply {
   if err:=ops.DeferGPU(ctx,op.GetOperationId(),op.GetDispatchToken(),"operator recovery of confirmed GPU wait; no Job or creation intent under shared admission lock");err!=nil{return err}
   result["applied"]=true
  }
  return json.NewEncoder(os.Stdout).Encode(result)
 })
}
