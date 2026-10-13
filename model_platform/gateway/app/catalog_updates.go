package app

import (
    "bytes"
    "context"
    "encoding/json"
    "io"
    "time"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    managementv1 "model-platform/gen/management/v1"
    deploymentv1 "model-platform/gen/deployment/v1"
)

func decodeLabelBody(c fiber.Ctx, dest any) error {
    if len(c.Body())>4096 { return status.Error(codes.InvalidArgument,"name request is too large") }
    dec:=json.NewDecoder(bytes.NewReader(c.Body()));dec.DisallowUnknownFields()
    if err:=dec.Decode(dest);err!=nil { return status.Error(codes.InvalidArgument,"invalid rename body") }
    if err:=dec.Decode(new(any));err!=io.EOF { return status.Error(codes.InvalidArgument,"invalid rename body") }
    return nil
}
func (a *API) renameModel(c fiber.Ctx) error {
    var body struct { Name string `json:"name"`; Expected string `json:"expected_name"` }
    if err:=decodeLabelBody(c,&body);err!=nil { return grpcError(c,err) }
    ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
    out,err:=a.clients.Management.RenameModel(ctx,&managementv1.RenameModelRequest{ModelId:c.Params("id"),Name:body.Name,ExpectedName:body.Expected})
    if err!=nil { return grpcError(c,err) };return c.JSON(modelListMap(out))
}
func (a *API) renameModelVersion(c fiber.Ctx) error {
    var body struct { Version string `json:"version"`; Expected string `json:"expected_version"` }
    if err:=decodeLabelBody(c,&body);err!=nil { return grpcError(c,err) }
    ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
    out,err:=a.clients.Management.RenameModelVersion(ctx,&managementv1.RenameModelVersionRequest{ModelVersionId:c.Params("id"),Version:body.Version,ExpectedVersion:body.Expected})
    if err!=nil { return grpcError(c,err) };return c.JSON(modelVersionListMap(out))
}
func (a *API) deleteDeployment(c fiber.Ctx) error {
    ctx,cancel:=context.WithTimeout(c.Context(),90*time.Second);defer cancel()
    out,err:=a.clients.Deployment.DeleteDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:c.Params("id")})
    if err!=nil { return grpcError(c,err) };return c.JSON(deploymentMap(out))
}
