package main

import "testing"

func testLaunchArgs()[]string{return []string{"run","-d","-e","OPERATION_ID=11111111-1111-4111-8111-111111111111","-e","RESOURCE_ID=22222222-2222-4222-8222-222222222222","-e","DISPATCH_TOKEN=33333333-3333-4333-8333-333333333333","-e","EXECUTION_TYPE=deployment.apply","-e","RESOURCE_TYPE=deployment","-e","REQUESTED_GPU_COUNT=0","-e","REQUESTED_GPU_IDS=","test-image"}}
func TestFaultShimAcceptsOnlyCPUDeployment(t *testing.T){
    e,err:=launchFromArgs(testLaunchArgs());if err!=nil||e.ExecutionType!="deployment.apply"||e.DispatchToken==""{t.Fatalf("%+v %v",e,err)}
    args:=append([]string{"run","--gpus","all"},testLaunchArgs()[1:]...)
    if _,err=launchFromArgs(args);err==nil{t.Fatal("GPU accepted")}
    args=testLaunchArgs();args[9]="EXECUTION_TYPE=training.run"
    if _,err=launchFromArgs(args);err==nil{t.Fatal("training accepted")}
    if _,err=launchFromArgs([]string{"rm","unrelated-container"});err==nil{t.Fatal("unrelated Docker mutation accepted")}
}
