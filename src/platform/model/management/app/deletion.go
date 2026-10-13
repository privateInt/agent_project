package app

import (
    "context"
    "strings"

    managementv1 "model-platform/gen/management/v1"
    "model-platform/internal/idempotency"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

// DeleteModel removes a catalog entry, not immutable model files. Existing
// imports, training output publication, deployments and provenance remain valid.
// UUID-scoped repeated DELETEs preserve the first deletion timestamp.
func (s *Service) DeleteModel(ctx context.Context, req *managementv1.GetModelRequest) (*managementv1.Model, error) {
    id, err := uuid.Parse(strings.TrimSpace(req.GetModelId()))
    if err != nil || id == uuid.Nil {
        return nil, status.Error(codes.InvalidArgument, "model_id must be UUID")
    }
    result, err := s.pool.Exec(ctx, `UPDATE models
        SET deleted_at=COALESCE(deleted_at,clock_timestamp()),
            updated_at=CASE WHEN deleted_at IS NULL THEN clock_timestamp() ELSE updated_at END
        WHERE model_id=$1`, id.String())
    if err != nil { return nil, idempotency.DBError(err) }
    if result.RowsAffected() == 0 { return nil, status.Error(codes.NotFound, "model not found") }
    return s.GetModel(ctx, &managementv1.GetModelRequest{ModelId:id.String()})
}
