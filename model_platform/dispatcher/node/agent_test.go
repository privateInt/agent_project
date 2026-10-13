package node

import (
 "context"
 "errors"
 "testing"
 "time"
 "github.com/google/uuid"
 commonv1 "model-platform/gen/common/v1"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
 "google.golang.org/protobuf/types/known/timestamppb"
)
type fakeOps struct{op *operationv1.Operation;err error}
func(f *fakeOps)Get(context.Context,string)(*operationv1.Operation,error){return f.op,f.err}
type profiles struct{}
func(profiles)Get(string)(execution.Profile,error){return execution.Profile{Image:"local-approved-image"},nil}
type runner struct{calls int;cmd execution.Command;err error}
func(r *runner)Run(_ context.Context,cmd execution.Command,_ execution.Profile)error{r.calls++;r.cmd=cmd;return r.err}
func fixture()(Agent,*fakeOps,*runner,remotenode.Request){
 op:=&operationv1.Operation{OperationId:uuid.NewString(),ResourceId:uuid.NewString(),ResourceType:"training_run",ExecutionType:"training.run",Kind:"training.run",DispatchProtocol:1,DispatchToken:uuid.NewString(),Status:"dispatching",DispatchLeaseUntil:timestamppb.New(time.Now().Add(time.Minute)),Resources:&commonv1.ExecutionResources{NodeName:"worker",GpuCount:1,GpuIds:[]int32{0}}}
 ops,run:=&fakeOps{op:op},&runner{}
 a:=Agent{Name:"worker",Operations:ops,Profiles:profiles{},Runner:run,Identity:func(context.Context)(string,string,error){return "daemon","amd64",nil},Check:func(context.Context,execution.Profile)error{return nil},Inspect:func(context.Context,*operationv1.Operation)(*remotenode.Evidence,error){return &remotenode.Evidence{State:"missing"},nil}}
 return a,ops,run,remotenode.Request{Version:1,Action:"launch",NodeName:"worker",DaemonID:"daemon",OperationID:op.OperationId,DispatchToken:op.DispatchToken}
}
func TestAgentUsesOperationTruthAndLocalProfile(t *testing.T){
 a,o,r,req:=fixture();out:=a.Handle(context.Background(),req)
 if out.Status!="accepted"||r.calls!=1||r.cmd.ResourceID!=o.op.ResourceId||r.cmd.Resources.NodeName!="worker"||r.cmd.DispatchToken!=req.DispatchToken{t.Fatal("launch contract mismatch",out)}
}
func TestAgentRejectsWrongNodeOrStaleGrantWithoutLaunch(t *testing.T){
 for _,change:=range []func(*Agent,*fakeOps,*remotenode.Request){
  func(_ *Agent,_ *fakeOps,r *remotenode.Request){r.NodeName="other"},
  func(_ *Agent,_ *fakeOps,r *remotenode.Request){r.DaemonID="other"},
  func(_ *Agent,_ *fakeOps,r *remotenode.Request){r.DispatchToken=uuid.NewString()},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.op.DispatchLeaseUntil=timestamppb.New(time.Now().Add(-time.Minute))},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.op.ExecutionStartedAt=timestamppb.Now();o.op.Status="running"},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.op.Resources.NodeName="other"},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.op.ExecutionType="deployment.serve"},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.op.Resources.GpuIds=nil},
  func(_ *Agent,o *fakeOps,_ *remotenode.Request){o.err=errors.New("offline")},
 }{a,o,r,req:=fixture();change(&a,o,&req);out:=a.Handle(context.Background(),req);if out.Status=="accepted"||out.Status=="busy"||r.calls!=0{t.Fatal("invalid request launched",out)}}
}
func TestAgentReplayAndUncertainInventoryDoNotRelaunch(t *testing.T){
 for _,unavailable:=range []bool{false,true}{a,_,r,req:=fixture();a.Inspect=func(context.Context,*operationv1.Operation)(*remotenode.Evidence,error){if unavailable{return nil,errors.New("offline")};return &remotenode.Evidence{State:"created"},nil}
  out:=a.Handle(context.Background(),req);if r.calls!=0{t.Fatal("replayed or unconfirmed launch repeated")};if unavailable&&out.Status!="unknown"{t.Fatal(out)};if !unavailable&&out.Status!="accepted"{t.Fatal(out)}
 }
}
func TestAgentOnlyConfirmedAdmissionConflictIsBusy(t *testing.T){
 for _,busy:=range []bool{false,true}{a,_,r,req:=fixture();r.err=errors.New("lost docker reply with SECRET=value");if busy{r.err=&gpuadmission.BusyError{Owner:"external"}}
  out:=a.Handle(context.Background(),req);if busy&&out.Status!="busy"{t.Fatal(out)};if !busy&&(out.Status!="unknown"||out.Message==r.err.Error()){t.Fatal("unknown launch leaked or released",out)}
 }
}
func TestRemoteEvidenceMustMatchNodeFenceAndGPU(t *testing.T){
 _,o,_,_:=fixture();labels:=map[string]string{"model-platform.owner":"operation-dispatcher","model-platform.operation_id":o.op.OperationId,"model-platform.dispatch_token":o.op.DispatchToken,"model-platform.execution_type":"training.run","model-platform.execution_node":"worker"}
 if _,e:=ValidateContainer(o.op,"container","image",labels,"exited",0,[]int32{0});e!=nil{t.Fatal(e)}
 for _,key:=range []string{"model-platform.owner","model-platform.operation_id","model-platform.dispatch_token","model-platform.execution_type","model-platform.execution_node"}{old:=labels[key];labels[key]="other";if _,e:=ValidateContainer(o.op,"container","image",labels,"exited",0,[]int32{0});e==nil{t.Fatal("mismatch accepted",key)};labels[key]=old}
 if _,e:=ValidateContainer(o.op,"container","image",labels,"exited",0,[]int32{1});e==nil{t.Fatal("wrong GPU accepted")}
}
