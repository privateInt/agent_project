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
type progressAPIClient struct{operationv1.OperationServiceClient}
func(c *progressAPIClient)GetOperation(context.Context,*operationv1.GetOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){
    return &operationv1.Operation{OperationId:"op",Status:"running",Phase:"transferring_files",DispatchToken:"not-public",ExecutionInstanceId:"not-public",Progress:&operationv1.TransferProgress{Stage:"downloading",CurrentFile:"model.safetensors",BytesCompleted:512,BytesTotal:1024,FilesTotal:3,Sequence:2},ProgressUpdatedAt:timestamppb.New(time.Date(2026,9,10,10,0,0,0,time.UTC))},nil
}
func TestOperationRESTExposesMeasuredTransferProgress(t *testing.T){
    app:=fiber.New();New(&grpcclient.Clients{Operation:&progressAPIClient{}}).Register(app)
    res,err:=app.Test(httptest.NewRequest("GET","/api/operations/op",nil));if err!=nil{t.Fatal(err)};defer res.Body.Close()
    var body map[string]any;if err=json.NewDecoder(res.Body).Decode(&body);err!=nil{t.Fatal(err)}
    p,ok:=body["progress"].(map[string]any)
    if res.StatusCode!=200||!ok||p["bytes_completed"]!=float64(512)||p["bytes_total"]!=float64(1024)||body["progress_updated_at"]!="2026-09-10T10:00:00Z"{t.Fatal(body)}
    if _,ok=body["dispatch_token"];ok{t.Fatal("execution token exposed")}
    if _,ok=body["execution_instance_id"];ok{t.Fatal("execution identity exposed")}
    if transferProgressMap(nil)!=nil{t.Fatal("old Jobs must have absent progress")}
}
