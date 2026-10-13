package main

import (
 "context"
 "encoding/json"
 "os"
 "path/filepath"
 "strings"
 "testing"
 commonv1 "model-platform/gen/common/v1"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/remotenode"
 "google.golang.org/grpc"
 "google.golang.org/protobuf/types/known/timestamppb"
)
type remoteTraining struct{trainingv1.TrainingServiceClient}
func(remoteTraining)GetTrainingRun(_ context.Context,r *trainingv1.GetTrainingRunRequest,_ ...grpc.CallOption)(*trainingv1.TrainingRun,error){return &trainingv1.TrainingRun{TrainingRunId:r.GetTrainingRunId(),Status:"running"},nil}
func TestRemoteReconciliationUsesRemoteEvidenceEvenWhenAdmissionDisabled(t *testing.T){
 dir:=t.TempDir();config,reply:=filepath.Join(dir,"nodes.json"),filepath.Join(dir,"reply")
 n:=remotenode.Node{Name:"worker",SSHHost:"worker-ssh",Container:"agent",Architecture:"amd64",DaemonID:"daemon",Enabled:false}
 raw,_:=json.Marshal(remotenode.Catalog{Version:1,Nodes:[]remotenode.Node{n}});if err:=os.WriteFile(config,raw,0600);err!=nil{t.Fatal(err)}
 script:=`#!/bin/sh
cat >/dev/null
cat "$REMOTE_RECONCILE_REPLY"
`
 if err:=os.WriteFile(filepath.Join(dir,"ssh"),[]byte(script),0700);err!=nil{t.Fatal(err)}
 t.Setenv("EXECUTION_NODES_FILE",config);t.Setenv("DISPATCHER_NODE_NAME","local");t.Setenv("PATH",dir+string(os.PathListSeparator)+os.Getenv("PATH"));t.Setenv("REMOTE_RECONCILE_REPLY",reply)
 op:=&operationv1.Operation{OperationId:"op",ResourceId:"run",ResourceType:"training_run",ExecutionType:"training.run",DispatchProtocol:1,ExecutionStartedAt:timestamppb.Now(),Resources:&commonv1.ExecutionResources{NodeName:"worker"}}
 response:=remotenode.Response{Version:1,Action:"inspect",OperationID:"op",NodeName:"worker",Architecture:"amd64",DaemonID:"daemon",Status:"observed",Evidence:&remotenode.Evidence{State:"running"}}
 raw,_=json.Marshal(response);if err:=os.WriteFile(reply,raw,0600);err!=nil{t.Fatal(err)}
 w:=worker{training:remoteTraining{},node:"local"};target,_:=w.observe(context.Background(),op);if target!="running"{t.Fatal("remote live Job not observed")}
 if err:=os.WriteFile(reply,[]byte("lost reply"),0600);err!=nil{t.Fatal(err)}
 target,note:=w.observe(context.Background(),op);if target!=""||!strings.Contains(note,"no restart or relocation"){t.Fatal("unavailable remote evidence was inferred locally",target,note)}
}
