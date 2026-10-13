package main
import (
 "testing"
 operationv1 "model-platform/gen/operation/v1"
)
func TestReconcileDecision(t *testing.T){
 for _,tc:=range []struct{domain,container,target string}{
 {"succeeded","missing","succeeded"},{"failed","running","failed"},
 {"unknown","running","running"},{"unknown","missing",""},
 {"unknown","exited",""},{"unknown","created",""},{"unknown","paused",""},
 }{got,_:=decision(tc.domain,tc.container);if got!=tc.target{t.Fatalf("%+v got=%s",tc,got)}}
}
func TestContainerIdentity(t *testing.T){
 op:=&operationv1.Operation{OperationId:"test-id",ExecutionType:"training.run"}
 e:=containerEvidence{Labels:map[string]string{"model-platform.owner":"operation-dispatcher","model-platform.operation_id":"other","model-platform.execution_type":"training.run"}}
 e.State.Running=true;e.State.Status="running"
 if _,err:=validateEvidence(op,e);err==nil{t.Fatal("accepted foreign container")}
 e.Labels["model-platform.operation_id"]="test-id"
 if s,err:=validateEvidence(op,e);err!=nil||s!="running"{t.Fatalf("%s %v",s,err)}
 e.State.Paused=true;e.State.Status="paused"
 if s,_:=validateEvidence(op,e);s=="running"{t.Fatal("paused container treated as running")}
}
