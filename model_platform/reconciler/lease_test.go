package main

import (
    "context"
    "strings"
    "testing"
    "time"

    operationv1 "model-platform/gen/operation/v1"
    "google.golang.org/grpc"
    "google.golang.org/protobuf/types/known/timestamppb"
)

type leaseOperations struct {
    operationv1.OperationServiceClient
    op *operationv1.Operation
    recoverRequest *operationv1.RecoverDispatchRequest
    applies int
}
func(f *leaseOperations)ListReconcileOperations(context.Context,*operationv1.ListReconcileOperationsRequest,...grpc.CallOption)(*operationv1.ListReconcileOperationsResponse,error){return &operationv1.ListReconcileOperationsResponse{Operations:[]*operationv1.Operation{f.op}},nil}
func(f *leaseOperations)RecoverDispatch(_ context.Context,r *operationv1.RecoverDispatchRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){f.recoverRequest=r;return &operationv1.Operation{Status:"pending"},nil}
func(f *leaseOperations)ApplyReconciliation(context.Context,*operationv1.ApplyReconciliationRequest,...grpc.CallOption)(*operationv1.Operation,error){f.applies++;return &operationv1.Operation{},nil}

func TestExpiredUnstartedLeaseIsRequeued(t *testing.T){
    op:=&operationv1.Operation{OperationId:"op",Status:"dispatching",DispatchProtocol:1,DispatchGeneration:1,DispatchToken:"token",DispatchLeaseUntil:timestamppb.New(time.Now().Add(-time.Minute))}
    f:=&leaseOperations{op:op};w:=worker{ops:f};w.cycle(context.Background())
    if f.recoverRequest.GetOperationId()!=op.GetOperationId()||f.recoverRequest.GetExpectedDispatchToken()!=op.GetDispatchToken()||f.applies!=0{t.Fatal("expired launch was not requeued before observation")}
}
func TestUnstartedContainerCannotBypassGate(t *testing.T){
    op:=&operationv1.Operation{OperationId:"op",ExecutionType:"training.run",DispatchProtocol:1,DispatchGeneration:1,DispatchToken:"token"}
    e:=containerEvidence{Labels:map[string]string{"model-platform.owner":"operation-dispatcher","model-platform.operation_id":"op","model-platform.execution_type":"training.run","model-platform.dispatch_token":"token"}}
    e.State.Running=true;e.State.Status="running"
    if _,err:=validateEvidence(op,e);err==nil{t.Fatal("Docker running bypassed execution grant")}
    // Nil domain clients also prove this branch performs no domain/Docker RPC.
    if target,_:=(worker{}).observe(context.Background(),op);target!=""{t.Fatal("unstarted job was repaired as running")}
    op.DispatchGeneration=3
    if target,note:=(worker{}).observe(context.Background(),op);target!=""||!strings.Contains(note,"budget exhausted"){t.Fatal("budget exhaustion not surfaced")}
    op.ExecutionStartedAt=timestamppb.Now()
    if s,err:=validateEvidence(op,e);err!=nil||s!="running"{t.Fatalf("current authorized container rejected: %s %v",s,err)}
    e.Labels["model-platform.dispatch_token"]="old-token"
    if _,err:=validateEvidence(op,e);err==nil{t.Fatal("old generation used as live evidence")}
}

func TestGPUWaitCreditsDoNotProduceExhaustedObservation(t *testing.T){
 op:=&operationv1.Operation{DispatchProtocol:1,Status:"dispatching",DispatchGeneration:9,GpuWaitCount:8}
 target,note:=(worker{}).observe(context.Background(),op)
 if target!=""||note!="execution authorization pending; waiting for lease recovery"{t.Fatal("GPU waits consumed launch budget",note)}
 op.GpuWaitCount=6
 _,note=(worker{}).observe(context.Background(),op)
 if note!="manual review required: pre-start recovery budget exhausted"{t.Fatal("real launch failures lost their limit",note)}
}
