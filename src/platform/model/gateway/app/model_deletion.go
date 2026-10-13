package app

import (
    "context"
    "time"
    managementv1 "model-platform/gen/management/v1"
    "github.com/gofiber/fiber/v3"
)

func (a *API) deleteModel(c fiber.Ctx) error {
    ctx, cancel := context.WithTimeout(c.Context(),15*time.Second)
    defer cancel()
    result, err := a.clients.Management.DeleteModel(ctx, &managementv1.GetModelRequest{ModelId:c.Params("id")})
    if err != nil { return grpcError(c,err) }
    return c.JSON(modelListMap(result))
}
