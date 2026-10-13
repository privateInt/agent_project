package app

import (
 "context"
 "encoding/json"
 "net/http/httptest"
 "testing"
 "time"
 "model-platform/gateway/grpcclient"
 operationv1 "model-platform/gen/operation/v1"
 "github.com/gofiber/fiber/v3"
 "google.golang.org/grpc"
 "google.golang.org/protobuf/types/known/timestamppb"
)
type operationStatusClient struct{operationv1.OperationServiceClient}
func(c *operationStatusClient)GetOperation(context.Context,*operationv1.GetOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){
 return &operationv1.Operation{OperationId:"op",Status:"pending",Phase:"waiting_resources",ErrorCode:"GPU_BUSY",ErrorMessage:"GPU reserved by contextrag-backend",DispatchGeneration:8,GpuWaitCount:8,GpuWaitUntil:timestamppb.New(time.Date(2026,9,9,8,0,0,0,time.UTC))},nil
}
func TestOperationRESTExposesGPUWaitAndRecoveryEvidence(t *testing.T){
 app:=fiber.New();New(&grpcclient.Clients{Operation:&operationStatusClient{}}).Register(app)
 res,err:=app.Test(httptest.NewRequest("GET","/api/operations/op",nil));if err!=nil{t.Fatal(err)};defer res.Body.Close()
 var body map[string]any;if err:=json.NewDecoder(res.Body).Decode(&body);err!=nil{t.Fatal(err)}
 if res.StatusCode!=200||body["phase"]!="waiting_resources"||body["error_code"]!="GPU_BUSY"||body["dispatch_generation"]!=float64(8)||body["gpu_wait_count"]!=float64(8)||body["gpu_wait_until"]!="2026-09-09T08:00:00Z"||body["execution_started_at"]!=nil{t.Fatal(body)}
}
