package gpuadmission

import (
 "context"
 "errors"
 "testing"
)
func TestUnstartedRecoveryRejectsIntentAndEveryJobState(t *testing.T){
 for _,mode:=range []string{"clear","intent","running","exited","foreign-name-label","inventory-error"}{t.Run(mode,func(t *testing.T){
  items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background();op:="same-op"
  if mode=="intent"{r:=req("old-job",0);r.OperationID=op;if err:=g.Run(ctx,r,func()error{return nil});err!=nil{t.Fatal(err)}}
  if mode=="running"||mode=="exited"{c:=active("model-op-"+op+"-old-token","0");c.State.Status=mode;c.State.Running=mode=="running";items=[]Container{c}}
  if mode=="foreign-name-label"{c:=active("other-name","0");c.Config.Labels=map[string]string{"model-platform.operation_id":op};items=[]Container{c}}
  if mode=="inventory-error"{g.Inventory=func(context.Context)([]Container,error){return nil,errors.New("offline")}}
  called:=false;err:=g.WithUnstartedProof(ctx,op,func()error{called=true;return nil})
  if mode=="clear"{if err!=nil||!called{t.Fatal(err)}}else if err==nil||called{t.Fatal("unproven recovery was allowed")}
 })}
}
