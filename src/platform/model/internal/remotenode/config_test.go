package remotenode

import (
 "encoding/json"
 "strings"
 "testing"
)
func testNode()Node{return Node{Name:"worker",SSHHost:"worker-ssh",Container:"model-node-agent",Architecture:"amd64",DaemonID:"daemon1",Enabled:true}}
func TestCatalogRejectsUnsafeOrAmbiguousPlacement(t *testing.T){
 for _,change:=range []func(*Node){
  func(n *Node){n.Name="local"},func(n *Node){n.SSHHost="host; touch /tmp/x"},
  func(n *Node){n.Container="-privileged"},func(n *Node){n.Architecture="arm64"},
  func(n *Node){n.DaemonID=""},
 } {n:=testNode();change(&n);raw,_:=json.Marshal(Catalog{Version:1,Nodes:[]Node{n}});if _,err:=Decode(raw,"local");err==nil{t.Fatal("unsafe node accepted",n)}}
 for _,field:=range []string{"name","ssh","daemon"}{
  a,b:=testNode(),testNode();b.Name="other";b.SSHHost="other-ssh";b.DaemonID="daemon2"
  switch field{case "name":b.Name=a.Name;case "ssh":b.SSHHost=a.SSHHost;case "daemon":b.DaemonID=a.DaemonID}
  raw,_:=json.Marshal(Catalog{Version:1,Nodes:[]Node{a,b}});if _,err:=Decode(raw,"local");err==nil{t.Fatal("duplicate accepted",field)}
 }
}
func TestCatalogBootstrapAndTrainingAdmission(t *testing.T){
 t.Setenv("DISPATCHER_NODE_NAME","local")
 n:=testNode();n.Enabled=false;n.DaemonID="";raw,_:=json.Marshal(Catalog{Version:1,Nodes:[]Node{n}})
 c,err:=Decode(raw,"local");if err!=nil{t.Fatal(err)}
 if c.ValidateTraining("worker",1)==nil{t.Fatal("unverified node admitted")}
 for _,name:=range []string{"","local"}{if err:=c.ValidateTraining(name,2);err!=nil{t.Fatal("local DDP changed",err)}}
 n=testNode();c.Nodes=[]Node{n};if c.ValidateTraining("worker",1)!=nil{t.Fatal("enabled node rejected")}
 if c.ValidateTraining("worker",2)==nil||c.ValidateTraining("missing",1)==nil{t.Fatal("unsupported placement accepted")}
}
func TestSSHArgumentsNeverCarryExecutionSecrets(t *testing.T){
 c:=Client{ConfigPath:"/node-ssh/config"};args,err:=c.Arguments(testNode());if err!=nil{t.Fatal(err)}
 joined:=strings.Join(args," ")
 for _,flag:=range []string{"StrictHostKeyChecking=yes","BatchMode=yes","ClearAllForwardings=yes","ForwardAgent=no"}{if !strings.Contains(joined,flag){t.Fatal("SSH policy missing",flag)}}
 if args[len(args)-1]!="docker exec -i model-node-agent /app/dispatcher --node-request"{t.Fatal("remote command changed")}
}
func TestRemoteResponseMustMatchRequestAndPinnedDaemon(t *testing.T){
 n:=testNode();req:=Request{Action:"launch",OperationID:"op"}
 good:=Response{Version:1,Action:"launch",OperationID:"op",NodeName:n.Name,Architecture:n.Architecture,DaemonID:n.DaemonID,Status:"accepted"}
 raw,_:=json.Marshal(good);if _,err:=DecodeResponse(raw,n,req);err!=nil{t.Fatal(err)}
 for _,change:=range []func(*Response){func(r *Response){r.NodeName="other"},func(r *Response){r.DaemonID="other"},func(r *Response){r.OperationID="other"},func(r *Response){r.Action="probe"},func(r *Response){r.Status="ok"}}{
  r:=good;change(&r);raw,_:=json.Marshal(r);if _,err:=DecodeResponse(raw,n,req);err==nil{t.Fatal("mismatched reply accepted")}
 }
 raw,_=json.Marshal(good);raw=append(raw,[]byte(` {"extra":true}`)...);if _,err:=DecodeResponse(raw,n,req);err==nil{t.Fatal("extra protocol data accepted")}
}
