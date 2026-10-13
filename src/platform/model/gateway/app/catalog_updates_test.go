package app

import (
    "context"
    "encoding/json"
    "net/http/httptest"
    "strings"
    "testing"
    "model-platform/gateway/grpcclient"
    managementv1 "model-platform/gen/management/v1"
    deploymentv1 "model-platform/gen/deployment/v1"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/types/known/timestamppb"
)
type renameClient struct { managementv1.ManagementServiceClient; err error; calls int }
func (c *renameClient) RenameModel(_ context.Context,r *managementv1.RenameModelRequest,_ ...grpc.CallOption)(*managementv1.Model,error) {
    c.calls++;if r.GetExpectedName()!="before" { panic("expected name lost") };return &managementv1.Model{ModelId:r.ModelId,Name:r.Name},c.err
}
func (c *renameClient) RenameModelVersion(_ context.Context,r *managementv1.RenameModelVersionRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error) {
    c.calls++;if r.GetExpectedVersion()!="before" { panic("expected version lost") };return &managementv1.ModelVersion{ModelVersionId:r.ModelVersionId,Version:r.Version},c.err
}
type deploymentDeletionClient struct { deploymentv1.DeploymentServiceClient; err error; id string }
func (c *deploymentDeletionClient) DeleteDeployment(_ context.Context,r *deploymentv1.GetDeploymentRequest,_ ...grpc.CallOption)(*deploymentv1.Deployment,error) {
    c.id=r.DeploymentId;return &deploymentv1.Deployment{DeploymentId:r.DeploymentId,DeletedAt:timestamppb.Now()},c.err
}
func TestRenameRESTValidationAndConflictMapping(t *testing.T) {
    for _,endpoint:=range []string{"models","model-versions"} {
        field:="name";if endpoint=="model-versions" { field="version" }
        for _,conflict:=range []bool{false,true} {
            client:=&renameClient{};if conflict { client.err=status.Error(codes.Aborted,"reload") }
            app:=fiber.New();New(&grpcclient.Clients{Management:client}).Register(app)
            body:=`{"`+field+`":"after","expected_`+field+`":"before"}`
            req:=httptest.NewRequest("PATCH","/api/"+endpoint+"/selected-id",strings.NewReader(body));req.Header.Set("Content-Type","application/json")
            resp,err:=app.Test(req);if err!=nil { t.Fatal(err) };resp.Body.Close()
            want:=200;if conflict { want=409 };if resp.StatusCode!=want || client.calls!=1 { t.Fatal(resp.StatusCode,client.calls) }
            for _,bad:=range []string{body+` {}`,`{"unknown":true}`,`{"name":12}`,strings.Repeat("x",4097)} {
                req=httptest.NewRequest("PATCH","/api/"+endpoint+"/selected-id",strings.NewReader(bad));req.Header.Set("Content-Type","application/json")
                resp,err=app.Test(req);if err!=nil { t.Fatal(err) };resp.Body.Close();if resp.StatusCode!=400 || client.calls!=1 { t.Fatal("invalid request reached RPC",resp.StatusCode,client.calls) }
            }
        }
    }
}
func TestDeploymentDeleteREST(t *testing.T) {
    for _,tc:=range []struct{err error; code int}{{nil,200},{status.Error(codes.FailedPrecondition,"stop first"),409},{status.Error(codes.NotFound,"missing"),404},{status.Error(codes.Unavailable,"offline"),503}} {
        client:=&deploymentDeletionClient{err:tc.err};app:=fiber.New();New(&grpcclient.Clients{Deployment:client}).Register(app)
        resp,err:=app.Test(httptest.NewRequest("DELETE","/api/deployments/selected-id",nil));if err!=nil { t.Fatal(err) }
        var body map[string]any;err=json.NewDecoder(resp.Body).Decode(&body);resp.Body.Close()
        if err!=nil || resp.StatusCode!=tc.code || client.id!="selected-id" { t.Fatal(resp.StatusCode,body,err) }
        if tc.code==200 && (body["deployment_id"]!="selected-id" || body["deleted_at"]==nil) { t.Fatal("deletion receipt lost",body) }
    }
}
