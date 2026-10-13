package app

import (
    "context"
    "encoding/json"
    "net/http/httptest"
    "testing"
    "time"
    "model-platform/gateway/grpcclient"
    managementv1 "model-platform/gen/management/v1"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/types/known/timestamppb"
)

type deletionClient struct {
    managementv1.ManagementServiceClient
    id string
    err error
}
func (c *deletionClient) DeleteModel(_ context.Context, req *managementv1.GetModelRequest, _ ...grpc.CallOption) (*managementv1.Model,error) {
    c.id=req.GetModelId()
    return &managementv1.Model{ModelId:c.id,Name:"example",DeletedAt:timestamppb.New(time.Date(2026,9,10,0,0,0,0,time.UTC))},c.err
}
func TestModelCatalogDeletionREST(t *testing.T) {
    for _,tc:=range []struct{name string;err error;code int}{
        {"deleted",nil,200}, {"missing",status.Error(codes.NotFound,"missing"),404},
        {"unavailable",status.Error(codes.Unavailable,"offline"),503},
    } {t.Run(tc.name,func(t *testing.T){
        client:=&deletionClient{err:tc.err}
        app:=fiber.New();New(&grpcclient.Clients{Management:client}).Register(app)
        id:="99999999-1111-4111-8111-111111111111"
        response,err:=app.Test(httptest.NewRequest("DELETE","/api/models/"+id,nil));if err!=nil { t.Fatal(err) };defer response.Body.Close()
        if response.StatusCode!=tc.code || client.id!=id { t.Fatal(response.StatusCode,client.id) }
        var body map[string]any
        if err=json.NewDecoder(response.Body).Decode(&body);err!=nil { t.Fatal(err) }
        if tc.code==200 && (body["model_id"]!=id || body["deleted_at"]!="2026-09-10T00:00:00Z") { t.Fatal("deletion fields lost",body) }
    })}
}
