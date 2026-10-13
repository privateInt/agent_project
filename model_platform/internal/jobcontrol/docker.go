// Package jobcontrol observes and stops local Training Jobs. Container identity
// comes from Operation and Docker labels; callers cannot supply a container ID.
package jobcontrol

import (
 "context"
 "encoding/json"
 "fmt"
 "io"
 "net"
 "net/http"
 "regexp"
 "strings"
 "time"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/gpuadmission"
)

var hexID=regexp.MustCompile(`^[0-9a-f]{64}$`)
var uuidID=regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)

type Runtime struct{Guard gpuadmission.Guard; HTTP *http.Client; Archive *ArchiveReader; Node string}
func New(node string,archive *ArchiveReader)*Runtime{
 transport:=&http.Transport{DialContext:func(ctx context.Context,_,_ string)(net.Conn,error){return (&net.Dialer{}).DialContext(ctx,"unix","/var/run/docker.sock")}}
 return &Runtime{Guard:gpuadmission.FromEnvironment(),HTTP:&http.Client{Transport:transport,Timeout:35*time.Second},Archive:archive,Node:strings.TrimSpace(node)}
}
func(r *Runtime)request(ctx context.Context,method,path string)(*http.Response,error){
 req,err:=http.NewRequestWithContext(ctx,method,"http://docker/v1.45"+path,nil);if err!=nil{return nil,err}
 return r.HTTP.Do(req)
}
type inspection struct{
 ID,Name string
 Config struct{Labels map[string]string; Tty bool}
 HostConfig struct{AutoRemove bool;RestartPolicy struct{Name string}}
 State struct{Status string;Running,Paused,Restarting bool}
}
func(r *Runtime)inspect(ctx context.Context,id string)(inspection,bool,error){
 var c inspection
 res,err:=r.request(ctx,"GET","/containers/"+id+"/json");if err!=nil{return c,false,err};defer res.Body.Close()
 if res.StatusCode==404{return c,false,nil}
 if res.StatusCode!=200{return c,false,fmt.Errorf("Docker inspect unavailable")}
 if err=json.NewDecoder(io.LimitReader(res.Body,2<<20)).Decode(&c);err!=nil{return c,false,fmt.Errorf("invalid Docker inspection")}
 return c,true,nil
}
func validate(c inspection,op *operationv1.Operation,node string,anyGeneration bool)error{
 if err:=validateIdentity(c,op,anyGeneration);err!=nil{return err}
 if c.Config.Labels["model-platform.execution_node"]!=node{return fmt.Errorf("training container identity mismatch")}
 return nil
}
func validateIdentity(c inspection,op *operationv1.Operation,anyGeneration bool)error{
 labels:=c.Config.Labels;token:=labels["model-platform.dispatch_token"]
 if !hexID.MatchString(c.ID)||!uuidID.MatchString(token)||c.Name!="/model-op-"+op.GetOperationId()+"-"+token ||
  labels["model-platform.owner"]!="operation-dispatcher"||labels["model-platform.operation_id"]!=op.GetOperationId()||labels["model-platform.execution_type"]!="training.run"||
  labels["model-platform.requested_node"]!=op.GetResources().GetNodeName()||(!anyGeneration&&token!=op.GetDispatchToken()){
  return fmt.Errorf("training container identity mismatch")
 }
 if c.Config.Tty||c.HostConfig.AutoRemove||(c.HostConfig.RestartPolicy.Name!=""&&c.HostConfig.RestartPolicy.Name!="no"){return fmt.Errorf("unsupported training container policy")}
 return nil
}
func(r *Runtime)Cancel(ctx context.Context,op *operationv1.Operation)error{
 if op.GetStatus()!="cancelling"||op.GetExecutionType()!="training.run"||op.GetResourceType()!="training_run"||!uuidID.MatchString(op.GetOperationId())||!uuidID.MatchString(op.GetResourceId())||
  (op.GetResources().GetNodeName()!=""&&op.GetResources().GetNodeName()!=r.Node){return fmt.Errorf("local cancelling training operation required")}
 return r.Guard.CancelOperation(ctx,op.GetOperationId(),func(ctx context.Context,item gpuadmission.Container)error{
  if !hexID.MatchString(item.ID){return fmt.Errorf("invalid container ID")}
  c,found,err:=r.inspect(ctx,item.ID);if err!=nil{return err};if !found{return nil}
  if c.ID!=item.ID{return fmt.Errorf("Docker inspection changed identity")}
  if err:=validate(c,op,r.Node,true);err!=nil{return err}
  if c.State.Status=="exited"&&!c.State.Running&&!c.State.Paused&&!c.State.Restarting{return nil}
  var method,path string
  if c.State.Status=="created"&&!c.State.Running&&!c.State.Paused&&!c.State.Restarting{
   method,path="DELETE","/containers/"+c.ID // no force, volumes or pruning
  }else if c.State.Status=="running"&&c.State.Running&&!c.State.Paused&&!c.State.Restarting{
   method,path="POST","/containers/"+c.ID+"/stop?t=10"
  }else{return fmt.Errorf("unsupported container state; stop requires review")}
  res,err:=r.request(ctx,method,path);if err!=nil{return fmt.Errorf("Docker stop unconfirmed")};defer res.Body.Close()
  if res.StatusCode!=204&&res.StatusCode!=304&&res.StatusCode!=404{return fmt.Errorf("Docker stop unconfirmed")}
  return nil // guard performs the final inventory before releasing claims
 })
}
func textUTF8(b []byte)string{return strings.ToValidUTF8(string(b),"�")}
