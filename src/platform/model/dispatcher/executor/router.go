package executor

import (
 "context"
 "fmt"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
)

type RemoteCaller interface { Call(context.Context,remotenode.Node,remotenode.Request)(remotenode.Response,error) }
type LocalRunner interface { Run(context.Context,execution.Command,execution.Profile)error }
type Router struct { Local LocalRunner; LocalName string; Nodes remotenode.Catalog; Remote RemoteCaller }
func(r Router)Run(ctx context.Context,cmd execution.Command,p execution.Profile)error{
 name:=cmd.Resources.NodeName
 if name==""||name==r.LocalName{return r.Local.Run(ctx,cmd,p)}
 n,ok:=r.Nodes.Find(name)
 if !ok||!n.Enabled{return &remotenode.RejectedError{Message:"requested remote node is not enabled"}}
 if cmd.ExecutionType!="training.run"||cmd.Resources.GPUCount!=1 {return &remotenode.RejectedError{Message:"remote node supports single-GPU training only"}}
 if r.Remote==nil{return fmt.Errorf("remote node client unavailable")}
 out,err:=r.Remote.Call(ctx,n,remotenode.Request{Action:"launch",OperationID:cmd.OperationID,DispatchToken:cmd.DispatchToken})
 if err!=nil{return err}
 switch out.Status {
 case "accepted":return nil
 case "busy":return &gpuadmission.BusyError{Owner:name+": "+out.Message}
 case "rejected":return &remotenode.RejectedError{Message:"remote node rejected launch: "+out.Message}
 default:return fmt.Errorf("remote launch outcome unconfirmed on %s",name)
 }
}
