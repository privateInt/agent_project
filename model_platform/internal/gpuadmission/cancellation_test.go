package gpuadmission

import (
 "context"
 "errors"
 "testing"
)

func TestCancellationFencesDelayedCreatorAndPreservesOtherGuards(t *testing.T){
 ctx:=context.Background();items:=[]Container{};g:=testGuard(t,&items)
 if err:=g.CancelOperation(ctx,"cancel-me",func(context.Context,Container)error{return nil});err!=nil{t.Fatal(err)}
 // Another service can update this file without losing the cancellation fence.
 other:=Guard{Dir:g.Dir,Inventory:g.Inventory}
 if err:=other.Run(ctx,req("other",1),func()error{return nil});err!=nil{t.Fatal(err)}
 called:=false;err:=other.Run(ctx,req("cancel-me",0),func()error{called=true;return nil})
 var cancelled *CancelledError
 if !errors.As(err,&cancelled)||called{t.Fatal("delayed creator escaped durable cancellation",err)}
}
func TestCancellationKeepsUncertainCreateAndRetriesAfterObservation(t *testing.T){
 ctx:=context.Background();items:=[]Container{};g:=testGuard(t,&items)
 request:=req("model-op-op-token",0);request.OperationID="op"
 _=g.Run(ctx,request,func()error{return errors.New("reply lost")})
 stop:=func(_ context.Context,c Container)error{items[0].State.Running=false;items[0].State.Status="exited";return nil}
 if g.CancelOperation(ctx,"op",stop)==nil{t.Fatal("missing container was treated as proof against uncertain create")}
 items=[]Container{active(request.Name,"0")}
 if err:=g.CancelOperation(ctx,"op",stop);err!=nil{t.Fatal(err)}
 if len(items)!=1||items[0].State.Status!="exited"{t.Fatal("exited evidence lost")}
 if err:=g.Run(ctx,req("new",0),func()error{return nil});err!=nil{t.Fatal("stopped GPU remained reserved",err)}
}
func TestCancellationStopFailureDoesNotReleaseReservation(t *testing.T){
 ctx:=context.Background();items:=[]Container{};g:=testGuard(t,&items)
 request:=req("model-op-op-token",0);request.OperationID="op"
 if err:=g.Run(ctx,request,func()error{items=[]Container{active(request.Name,"0")};return nil});err!=nil{t.Fatal(err)}
 if g.CancelOperation(ctx,"op",func(context.Context,Container)error{return errors.New("daemon unavailable")})==nil{t.Fatal("stop failure hidden")}
 if g.Run(ctx,req("new",0),func()error{t.Fatal("GPU reused before stop");return nil})==nil{t.Fatal("reservation released")}
}
