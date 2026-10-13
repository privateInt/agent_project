package node

import (
 "context"
 "encoding/json"
 "fmt"
 "net"
 "net/http"
 "os"
 "os/exec"
 "strconv"
 "strings"
 "time"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "model-platform/internal/remotenode"
)
func docker(ctx context.Context,args ...string)([]byte,error){
 c,cancel:=context.WithTimeout(ctx,5*time.Second);defer cancel()
 data,err:=exec.CommandContext(c,"docker",args...).Output()
 if err!=nil{return nil,fmt.Errorf("Docker observation unavailable")};return data,nil
}
func Identity(ctx context.Context)(string,string,error){
 data,err:=docker(ctx,"info","--format",`{"id":{{json .ID}},"arch":{{json .Architecture}},"os":{{json .OSType}}}`)
 var result struct{ID,Arch,OS string}
 if err!=nil||json.Unmarshal(data,&result)!=nil||result.ID==""||result.OS!="linux"{return "","",fmt.Errorf("Linux Docker identity unavailable")}
 if result.Arch=="x86_64"{result.Arch="amd64"};if result.Arch=="aarch64"{result.Arch="arm64"}
 return result.ID,result.Arch,nil
}
func Check(ctx context.Context,p execution.Profile)error{
 if os.Getenv("JOB_NETWORK")!="host"||os.Getenv("GPU_ADMISSION_DIR")==""||os.Getenv("DOCKER_HOST")!=""||os.Getenv("DOCKER_CONTEXT")!="" {return fmt.Errorf("node requires local Docker, host network and local admission volume")}
 raw,err:=docker(ctx,"image","inspect","--format",`{"arch":{{json .Architecture}},"labels":{{json .Config.Labels}}}`,p.Image)
 var meta struct{Arch string;Labels map[string]string}
 if err!=nil||json.Unmarshal(raw,&meta)!=nil||meta.Arch!="amd64"||meta.Labels["model-platform.execution-gate"]!="1" {return fmt.Errorf("local amd64 execution-gated training image unavailable")}
 for _,key:=range []string{"MINIO_ACCESS_KEY","MINIO_SECRET_KEY"}{if os.Getenv(key)==""{return fmt.Errorf("node storage credentials missing")}}
 for _,key:=range []string{"OPERATION_GRPC_ADDRESS","TRAINING_GRPC_ADDRESS","MANAGEMENT_GRPC_ADDRESS"}{
  address:=os.Getenv(key);host,_,err:=net.SplitHostPort(address)
  if err!=nil||host!="127.0.0.1" {return fmt.Errorf("node control endpoints must use loopback SSH forwards")}
  conn,err:=(&net.Dialer{Timeout:2*time.Second}).DialContext(ctx,"tcp",address);if err!=nil{return fmt.Errorf("node control tunnel unavailable")};conn.Close()
 }
 endpoint:=os.Getenv("MINIO_ENDPOINT");host,_,err:=net.SplitHostPort(endpoint)
 if err!=nil||host!="127.0.0.1"||os.Getenv("MINIO_USE_SSL")!="false" {return fmt.Errorf("node MinIO endpoint must use loopback SSH forward")}
 urls:=[]string{"http://"+endpoint+"/minio/health/live",strings.TrimRight(os.Getenv("MLFLOW_TRACKING_URI"),"/")+"/health"}
 for _,address:=range urls {
  req,err:=http.NewRequestWithContext(ctx,http.MethodGet,address,nil)
  if err!=nil||req.URL.Scheme!="http"||req.URL.Hostname()!="127.0.0.1"{return fmt.Errorf("node storage/metrics endpoint invalid")}
  client:=http.Client{Timeout:3*time.Second,CheckRedirect:func(*http.Request,[]*http.Request)error{return http.ErrUseLastResponse}}
  res,err:=client.Do(req);if err!=nil{return fmt.Errorf("node storage/metrics tunnel unavailable")};res.Body.Close()
  if res.StatusCode!=200{return fmt.Errorf("node storage/metrics health check failed")}
 }
 return nil
}
func Inspect(ctx context.Context,op *operationv1.Operation)(*remotenode.Evidence,error){
 // Inventory determines absence without parsing localised Docker CLI errors.
 items,err:=gpuadmission.DockerInventory(ctx);if err!=nil{return nil,err}
 name:="model-op-"+op.GetOperationId()+"-"+op.GetDispatchToken()
 found:="";for _,c:=range items{if strings.TrimPrefix(c.Name,"/")==name{found=c.ID;break}}
 if found==""{return &remotenode.Evidence{State:"missing"},nil}
 // Restricted inspect: no environment, command, mount configuration, or logs.
 raw,err:=docker(ctx,"inspect","--type","container","--format",`{"ID":{{json .Id}},"Image":{{json .Image}},"Labels":{{json .Config.Labels}},"State":{{json .State}},"Devices":{{json .HostConfig.DeviceRequests}}}`,found)
 var c struct{ID,Image string;Labels map[string]string;State struct{Status string;Running,Paused,Restarting bool;ExitCode int};Devices []gpuadmission.DeviceRequest}
 if err!=nil||json.Unmarshal(raw,&c)!=nil{return nil,fmt.Errorf("container evidence unavailable")}
 var ids []int32
 for _,d:=range c.Devices{for _,v:=range d.DeviceIDs {i,err:=strconv.ParseInt(v,10,32);if err!=nil{return nil,fmt.Errorf("non-index GPU selector")};ids=append(ids,int32(i))}}
 state:=c.State.Status
 if state=="running"&&(!c.State.Running||c.State.Paused||c.State.Restarting){return nil,fmt.Errorf("container state inconsistent")}
 return ValidateContainer(op,c.ID,c.Image,c.Labels,state,c.State.ExitCode,ids)
}
