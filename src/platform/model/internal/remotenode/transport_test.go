package remotenode

import (
 "context"
 "encoding/json"
 "os"
 "path/filepath"
 "strings"
 "testing"
)
func TestSSHProtocolUsesStdinAndLostReplyStaysUnconfirmed(t *testing.T){
 dir:=t.TempDir();argsPath,inputPath,replyPath:=filepath.Join(dir,"args"),filepath.Join(dir,"input"),filepath.Join(dir,"reply")
 script:=`#!/bin/sh
printf '%s\n' "$@" > "$NODE_TEST_ARGS"
cat > "$NODE_TEST_INPUT"
if [ "$NODE_TEST_LOST" = yes ]; then
  printf '%s\n' 'sensitive diagnostic' >&2
  exit 255
fi
cat "$NODE_TEST_REPLY"
`
 if err:=os.WriteFile(filepath.Join(dir,"ssh"),[]byte(script),0700);err!=nil{t.Fatal(err)}
 t.Setenv("PATH",dir+string(os.PathListSeparator)+os.Getenv("PATH"));t.Setenv("NODE_TEST_ARGS",argsPath);t.Setenv("NODE_TEST_INPUT",inputPath);t.Setenv("NODE_TEST_REPLY",replyPath)
 n:=testNode();reply:=Response{Version:1,Action:"launch",NodeName:n.Name,DaemonID:n.DaemonID,Architecture:n.Architecture,OperationID:"op",Status:"accepted"};raw,_:=json.Marshal(reply)
 if err:=os.WriteFile(replyPath,raw,0600);err!=nil{t.Fatal(err)}
 c:=Client{ConfigPath:"/node-ssh/config"};req:=Request{Action:"launch",OperationID:"op",DispatchToken:"private-token"}
 if _,err:=c.Call(context.Background(),n,req);err!=nil{t.Fatal(err)}
 args,_:=os.ReadFile(argsPath);input,_:=os.ReadFile(inputPath)
 if strings.Contains(string(args),req.DispatchToken)||!strings.Contains(string(input),req.DispatchToken){t.Fatal("dispatch token did not use encrypted stdin exclusively")}
 t.Setenv("NODE_TEST_LOST","yes");if _,err:=c.Call(context.Background(),n,req);err==nil||strings.Contains(err.Error(),"sensitive"){t.Fatal("lost reply accepted or stderr leaked",err)}
}
