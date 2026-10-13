package remotenode

import (
 "bytes"
 "context"
 "encoding/json"
 "fmt"
 "io"
 "os"
 "os/exec"
)

// DispatchToken is sent through encrypted stdin, never a command argument,
// Kafka field, user report, or container environment inspection.
type Request struct {
 Version int `json:"version"`
 Action string `json:"action"`
 NodeName string `json:"node_name"`
 DaemonID string `json:"daemon_id"`
 OperationID string `json:"operation_id,omitempty"`
 DispatchToken string `json:"dispatch_token,omitempty"`
}
type Evidence struct {
 ContainerID string `json:"container_id"`
 ImageID string `json:"image_id"`
 State string `json:"state"`
 ExitCode int `json:"exit_code"`
 GPUIds []int32 `json:"gpu_ids"`
}
type Response struct {
 Version int `json:"version"`
 Action string `json:"action"`
 NodeName string `json:"node_name"`
 DaemonID string `json:"daemon_id"`
 Architecture string `json:"architecture"`
 OperationID string `json:"operation_id,omitempty"`
 Status string `json:"status"`
 Message string `json:"message,omitempty"`
 Evidence *Evidence `json:"evidence,omitempty"`
}
type RejectedError struct{ Message string }
func(e *RejectedError)Error()string{return e.Message}
type Client struct { ConfigPath string }
func NewClient()Client{p:=os.Getenv("NODE_SSH_CONFIG");if p==""{p="/node-ssh/config"};return Client{ConfigPath:p}}
func(c Client)Arguments(n Node) ([]string,error){
 if !ValidName(n.Name)||!ValidName(n.SSHHost)||!ValidName(n.Container)||c.ConfigPath=="" {return nil,fmt.Errorf("invalid SSH node identity")}
 // OpenSSH sends its remote command through a shell. Only validated names and
 // this fixed command enter argv; the request itself is exclusively stdin.
 return []string{"-T","-F",c.ConfigPath,"-o","BatchMode=yes","-o","StrictHostKeyChecking=yes","-o","IdentitiesOnly=yes","-o","ForwardAgent=no","-o","ClearAllForwardings=yes","-o","ConnectTimeout=5","-o","ServerAliveInterval=5","-o","ServerAliveCountMax=2",n.SSHHost,"docker exec -i "+n.Container+" /app/dispatcher --node-request"},nil
}
type limitedBuffer struct{ bytes.Buffer }
func(b *limitedBuffer)Write(p []byte)(int,error){if b.Len()+len(p)>64<<10{return 0,fmt.Errorf("remote response too large")};return b.Buffer.Write(p)}
func(c Client)Call(ctx context.Context,n Node,r Request)(Response,error){
 args,err:=c.Arguments(n);if err!=nil{return Response{},err}
 r.Version=1;r.NodeName=n.Name;r.DaemonID=n.DaemonID
 data,err:=json.Marshal(r);if err!=nil{return Response{},err}
 cmd:=exec.CommandContext(ctx,"ssh",args...);cmd.Stdin=bytes.NewReader(data)
 var output limitedBuffer;cmd.Stdout=&output;cmd.Stderr=io.Discard
 if err=cmd.Run();err!=nil{return Response{},fmt.Errorf("remote SSH request failed for node %s; outcome unconfirmed",n.Name)}
 return DecodeResponse(output.Bytes(),n,r)
}
func DecodeResponse(data []byte,n Node,r Request)(Response,error){
 var out Response;d:=json.NewDecoder(bytes.NewReader(data));d.DisallowUnknownFields()
 if d.Decode(&out)!=nil||d.Decode(new(any))!=io.EOF||out.Version!=1||out.NodeName!=n.Name||out.Action!=r.Action||out.OperationID!=r.OperationID||out.Architecture!=n.Architecture||out.DaemonID=="" {return out,fmt.Errorf("remote response identity/protocol mismatch")}
 if n.DaemonID!=""&&out.DaemonID!=n.DaemonID{return out,fmt.Errorf("remote Docker daemon identity changed")}
 switch out.Status {case "ready","accepted","observed","busy","rejected","unknown":default:return out,fmt.Errorf("unknown remote response status")}
 return out,nil
}
