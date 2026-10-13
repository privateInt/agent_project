package app

import (
    "context"
    "encoding/json"
    "net/http/httptest"
    "strings"
    "testing"
    "time"
    "model-platform/gateway/grpcclient"
    managementv1 "model-platform/gen/management/v1"
    operationv1 "model-platform/gen/operation/v1"
    trainingv1 "model-platform/gen/training/v1"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/protobuf/types/known/timestamppb"
)

type listModelClient struct { managementv1.ManagementServiceClient; request *managementv1.ListModelsRequest }
func (c *listModelClient) ListModels(_ context.Context,r *managementv1.ListModelsRequest,_ ...grpc.CallOption)(*managementv1.ListModelsResponse,error){
    c.request=r
    if r.GetQ()=="empty" {return &managementv1.ListModelsResponse{},nil}
    return &managementv1.ListModelsResponse{Models:[]*managementv1.Model{{ModelId:"id",Name:"테스트",ModelType:"llm",CreatedAt:timestamppb.New(time.Date(2026,9,9,8,0,0,0,time.UTC))}},NextCursor:"next"},nil
}
func TestListRESTParametersAndEmptyArray(t *testing.T){
    client:=&listModelClient{};app:=fiber.New();New(&grpcclient.Clients{Management:client}).Register(app)
    for _,path:=range []string{"/api/models?q=%ED%85%8C%EC%8A%A4%ED%8A%B8&model_type=llm&limit=2&cursor=old","/api/models?q=empty"}{
        response,err:=app.Test(httptest.NewRequest("GET",path,nil));if err!=nil{t.Fatal(err)}
        var body map[string]any;err=json.NewDecoder(response.Body).Decode(&body);response.Body.Close();if err!=nil||response.StatusCode!=200{t.Fatal(response.StatusCode,body,err)}
        items,ok:=body["items"].([]any);if !ok{t.Fatalf("items must always be an array: %v",body)}
        if client.request.GetQ()=="empty" {if len(items)!=0 || body["next_cursor"]!="" || client.request.GetLimit()!=20{t.Fatal(body)};continue}
        if client.request.GetQ()!="테스트" || client.request.GetModelType()!="llm" || client.request.GetLimit()!=2 || client.request.GetCursor()!="old" {t.Fatal(client.request)}
        if len(items)!=1 || items[0].(map[string]any)["created_at"]!="2026-09-09T08:00:00Z" || body["next_cursor"]!="next" {t.Fatal(body)}
    }
}
func TestListRESTRejectsMalformedAndUnknownQueryBeforeRPC(t *testing.T){
    app:=fiber.New();New(&grpcclient.Clients{}).Register(app)
    for _,query:=range []string{"limit=0","limit=101","limit=", "limit=oops", "limit=1&limit=2","q=a&q=b","sort=name","limt=20","cursor="+strings.Repeat("a",2049)} {
        t.Run(query[:min(len(query),40)],func(t *testing.T){res,err:=app.Test(httptest.NewRequest("GET","/api/models?"+query,nil));if err!=nil{t.Fatal(err)};res.Body.Close();if res.StatusCode!=400{t.Fatal(res.StatusCode)}})
    }
}
func TestListSummaryExcludesExecutionGrantsAndLargeResults(t *testing.T){
    operation:=operationListMap(&operationv1.Operation{DispatchToken:"secret",DispatchOwner:"owner",ExecutionInstanceId:"instance",ErrorCode:"GPU_BUSY",Phase:"waiting_resources"})
    for _,key:=range []string{"dispatch_token","dispatch_owner","execution_instance_id"}{if _,ok:=operation[key];ok{t.Fatal("execution grant exposed",key)}}
    if operation["phase"]!="waiting_resources" || operation["error_code"]!="GPU_BUSY"{t.Fatal(operation)}
    training:=trainingListMap(&trainingv1.TrainingRun{ResultJson:"large result"})
    for _,key:=range []string{"result_json","result"}{if _,ok:=training[key];ok{t.Fatal("large result in list",key)}}
}
