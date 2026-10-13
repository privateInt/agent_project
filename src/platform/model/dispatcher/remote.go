package main

import (
 "context"
 "encoding/json"
 "fmt"
 "io"
 "os"
 "time"
 "github.com/google/uuid"
 dispatchapp "model-platform/dispatcher/app"
 "model-platform/dispatcher/executor"
 "model-platform/dispatcher/node"
 "model-platform/dispatcher/registry"
 "model-platform/internal/remotenode"
)

func nodeRequest()error{
 ctx,cancel:=context.WithTimeout(context.Background(),25*time.Second);defer cancel()
 var req remotenode.Request
 d:=json.NewDecoder(io.LimitReader(os.Stdin,8193));d.DisallowUnknownFields()
 if d.Decode(&req)!=nil||d.Decode(new(any))!=io.EOF{return fmt.Errorf("invalid node request")}
 name:=os.Getenv("NODE_AGENT_NAME")
 if !remotenode.ValidName(name){return fmt.Errorf("NODE_AGENT_NAME is required")}
 ops,err:=dispatchapp.ConnectOperation(env("OPERATION_GRPC_ADDRESS","127.0.0.1:19001"));if err!=nil{return err};defer ops.Close()
 agent:=node.Agent{Name:name,Operations:ops,Profiles:registry.New(),Runner:executor.NewDocker("host",name),Identity:node.Identity,Check:node.Check,Inspect:node.Inspect}
 return json.NewEncoder(os.Stdout).Encode(agent.Handle(ctx,req))
}
func nodeCLI(args []string)error{
 if len(args)!=2{return fmt.Errorf("use --probe-node NAME or --inspect-node-job OPERATION_UUID")}
 catalog,err:=remotenode.Load();if err!=nil{return err}
 req:=remotenode.Request{Action:"probe"};name:=args[1]
 ctx,cancel:=context.WithTimeout(context.Background(),35*time.Second);defer cancel()
 if args[0]=="--inspect-node-job" {
  id,err:=uuid.Parse(args[1]);if err!=nil||id==uuid.Nil{return fmt.Errorf("valid Operation UUID required")}
  ops,err:=dispatchapp.ConnectOperation(env("OPERATION_GRPC_ADDRESS","operation:9000"));if err!=nil{return err};defer ops.Close()
  op,err:=ops.Get(ctx,id.String());if err!=nil{return err}
  name=op.GetResources().GetNodeName();req.Action="inspect";req.OperationID=id.String()
 } else if args[0]!="--probe-node" {return fmt.Errorf("unsupported node command")}
 n,ok:=catalog.Find(name);if !ok{return fmt.Errorf("node is not registered")}
 out,err:=remotenode.NewClient().Call(ctx,n,req);if err!=nil{return err}
 if err=json.NewEncoder(os.Stdout).Encode(out);err!=nil{return err}
 if (req.Action=="probe"&&out.Status!="ready")||(req.Action=="inspect"&&out.Status!="observed"){return fmt.Errorf("node verification did not pass")}
 return nil
}
