package operationreport
import (
 "context"
 "testing"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
func TestPermanentFailureIsNotRetried(t *testing.T) {
 calls:=0
 _,err:=report(context.Background(),"test","id",func(context.Context)(*operationv1.Operation,error){calls++;return nil,status.Error(codes.FailedPrecondition,"terminal")})
 if status.Code(err)!=codes.FailedPrecondition || calls!=1 {t.Fatalf("calls=%d err=%v",calls,err)}
}
func TestTransientFailureRecovers(t *testing.T) {
 calls:=0
 result,err:=report(context.Background(),"test","id",func(context.Context)(*operationv1.Operation,error){calls++;if calls==1{return nil,status.Error(codes.Unavailable,"lost response")};return &operationv1.Operation{Status:"succeeded"},nil})
 if err!=nil || calls!=2 || result.GetStatus()!="succeeded" {t.Fatalf("calls=%d err=%v",calls,err)}
}
func TestRetryBudget(t *testing.T) {
 calls:=0
 _,err:=report(context.Background(),"test","id",func(context.Context)(*operationv1.Operation,error){calls++;return nil,status.Error(codes.Unavailable,"offline")})
 if calls!=3 || status.Code(err)!=codes.Unavailable {t.Fatalf("calls=%d err=%v",calls,err)}
}
