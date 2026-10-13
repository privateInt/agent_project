package main

import("testing";"time")

func TestCancelledJobsUseFailureRetention(t *testing.T){
 c,op,policy:=fixture();op.Status="cancelled"
 snapshot,err:=policy.candidate(c,op,testNow);if err!=nil{t.Fatal(err)}
 policy.SuccessKeep=time.Hour;policy.FailureKeep=7*24*time.Hour
 if policy.removable(snapshot,testNow.Add(2*time.Hour)){t.Fatal("cancelled logs received success retention")}
 if !policy.removable(snapshot,testNow.Add(8*24*time.Hour)){t.Fatal("cancelled logs never expire")}
}
