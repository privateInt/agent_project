package app

import (
 "context"
 "errors"
 "testing"
 "time"
 "model-platform/internal/gpuadmission"
 "google.golang.org/protobuf/types/known/timestamppb"
)
type waitOps struct{*fakeOps;waits int;waitErr error;token,reason string}
func(o *waitOps)DeferGPU(_ context.Context,id,token,reason string)error{o.waits++;o.token=token;o.reason=reason;return o.waitErr}
func TestConfirmedGPUWaitCanCommitCurrentRecord(t *testing.T){
 d,o,_,r:=fixture("dispatching",true);w:=&waitOps{fakeOps:o};d.Operations=w;r.err=&gpuadmission.BusyError{Owner:"contextrag-backend"}
 if err:=d.Handle(context.Background(),testPayload);err!=nil{t.Fatal(err)}
 if w.waits!=1||w.fails!=0||w.token!=o.result.Operation.DispatchToken||w.reason!="GPU reserved by contextrag-backend"{t.Fatal("GPU wait was not durably deferred")}
}
func TestUnconfirmedWaitOrUnknownLaunchRetainsRecord(t *testing.T){
 for _,unknown:=range []bool{false,true}{d,o,_,r:=fixture("dispatching",true);w:=&waitOps{fakeOps:o,waitErr:errors.New("DB reply lost")};d.Operations=w
  r.err=&gpuadmission.BusyError{Owner:"busy"};if unknown{r.err=errors.New("Docker reply lost")}
  if d.Handle(context.Background(),testPayload)==nil||w.fails!=0{t.Fatal("uncertain work acknowledged")}
  if unknown&&w.waits!=0{t.Fatal("unknown Docker launch treated as GPU wait")}
 }
}
func TestEarlyGPUWaitDuplicateUsesDurableFutureDelivery(t *testing.T){
 d,o,_,r:=fixture("pending",false);o.result.Operation.Phase="waiting_resources";o.result.Operation.GpuWaitUntil=timestamppb.New(time.Now().Add(time.Minute))
 if err:=d.Handle(context.Background(),testPayload);err!=nil||r.calls!=0{t.Fatal("future scheduled record not recognized",err)}
 o.result.Operation.GpuWaitUntil=nil
 if d.Handle(context.Background(),testPayload)==nil{t.Fatal("missing future delivery silently acknowledged")}
}
