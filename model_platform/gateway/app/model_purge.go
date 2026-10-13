package app

import (
	"context"
	"github.com/gofiber/fiber/v3"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	managementv1 "model-platform/gen/management/v1"
	"time"
)

func (a *API) purgeModel(c fiber.Ctx) error {
	id := c.Params("id")
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id || len(c.Body()) != 0 || len(c.Request().URI().QueryString()) != 0 {
		return grpcError(c, status.Error(codes.InvalidArgument, "model purge requires one UUID with no query or body"))
	}
	ctx, cancel := context.WithTimeout(c.Context(), 90*time.Second)
	defer cancel()
	result, e := a.clients.Management.PurgeModel(ctx, &managementv1.GetModelRequest{ModelId: id})
	if e != nil {
		return grpcError(c, e)
	}
	if result.GetModelId() != id || result.GetPurgedAt() == nil || result.GetPurgedAt().CheckValid() != nil || result.GetPurgeState() != "purged" {
		return grpcError(c, status.Error(codes.DataLoss, "invalid permanent deletion response"))
	}
	return c.JSON(modelListMap(result))
}
