package executor

import (
 "context"
 "errors"
 "testing"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
)
type localFake struct{calls int}
func(l *localFake)Run(context.Context,execution.Command,execution.Profile)error{l.calls++;return nil}
type remoteFake struct{calls int;status string;err error;node remotenode.Node;request remotenode.Request}
func(r *remoteFake)Call(_ context.Context,n remotenode.Node,q remotenode.Request)(remotenode.Response,error){r.calls++;r.node=n;r.request=q;return remotenode.Response{Status:r.status,Message:"owner"},r.err}
func TestExplicitRemoteRoutingNeverFallsBackToLocal(t *testing.T){
 for _,remoteErr:=range []bool{false,true}{l,r:=&localFake{},&remoteFake{status:"accepted"};if remoteErr{r.err=errors.New("SSH lost")}
  router:=Router{Local:l,LocalName:"local",Nodes:remotenode.Catalog{Nodes:[]remotenode.Node{{Name:"worker",Enabled:true}}},Remote:r}
  cmd:=execution.Command{OperationID:"op",DispatchToken:"secret",ExecutionType:"training.run",Resources:execution.Resources{NodeName:"worker",GPUCount:1,GPUIds:[]int32{0}}}
  err:=router.Run(context.Background(),cmd,execution.Profile{});if (err!=nil)!=remoteErr||l.calls!=0||r.calls!=1||r.request.DispatchToken!="secret"{t.Fatal("routing failure")}
  cmd.Resources.NodeName="";if err:=router.Run(context.Background(),cmd,execution.Profile{});err!=nil||l.calls!=1{t.Fatal("local path changed")}
 }
}
func TestRemoteBusyAndRejectedRemainDistinctFromAmbiguity(t *testing.T){
 l,r:=&localFake{},&remoteFake{};router:=Router{Local:l,Nodes:remotenode.Catalog{Nodes:[]remotenode.Node{{Name:"worker",Enabled:true}}},Remote:r}
 cmd:=execution.Command{ExecutionType:"training.run",Resources:execution.Resources{NodeName:"worker",GPUCount:1}}
 for _,s:=range []string{"busy","rejected","unknown"}{r.status=s;err:=router.Run(context.Background(),cmd,execution.Profile{});var busy *gpuadmission.BusyError;var rejected *remotenode.RejectedError
  if err==nil||errors.As(err,&busy)!=(s=="busy")||errors.As(err,&rejected)!=(s=="rejected"){t.Fatal("outcome misclassified",s,err)}
 }
 cmd.ExecutionType="deployment.serve";r.calls=0;if router.Run(context.Background(),cmd,execution.Profile{})==nil||r.calls!=0||l.calls!=0{t.Fatal("unsupported remote serving launched")}
}
