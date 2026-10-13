package app

import (
    "context"
    "errors"
    "strings"
    "unicode"
    "unicode/utf8"

    "github.com/google/uuid"
    "github.com/jackc/pgx/v5"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    managementv1 "model-platform/gen/management/v1"
    "model-platform/internal/idempotency"
)

func renameInput(rawID, rawLabel, expected string, limit int) (string, string, error) {
    id, err := uuid.Parse(strings.TrimSpace(rawID))
    label := strings.TrimSpace(rawLabel)
    if err != nil || id == uuid.Nil || label == "" || !utf8.ValidString(label) || utf8.RuneCountInString(label)>limit || strings.IndexFunc(label,unicode.IsControl)>=0 || expected=="" {
        return "", "", status.Error(codes.InvalidArgument,"a valid ID, display name and expected current name are required")
    }
    return id.String(),label,nil
}

// Lock the parent first, in the same order as import and model purge. Labels
// are metadata: UUIDs, source revisions and immutable artifact paths never change.
func renameParent(ctx context.Context, tx pgx.Tx, id string) (string,error) {
    var name string
    var unavailable bool
    err:=tx.QueryRow(ctx,`SELECT name,deleted_at IS NOT NULL OR purge_state<>'' FROM models WHERE model_id=$1 FOR UPDATE`,id).Scan(&name,&unavailable)
    if errors.Is(err,pgx.ErrNoRows) { return "",status.Error(codes.NotFound,"model not found") }
    if err!=nil { return "",idempotency.DBError(err) }
    if unavailable { return "",status.Error(codes.FailedPrecondition,"model was removed or is being purged") }
    return name,nil
}
func (s *Service) RenameModel(ctx context.Context, req *managementv1.RenameModelRequest) (*managementv1.Model,error) {
    id,name,err:=renameInput(req.GetModelId(),req.GetName(),req.GetExpectedName(),200)
    if err!=nil { return nil,err }
    tx,err:=s.pool.Begin(ctx);if err!=nil { return nil,idempotency.DBError(err) };defer idempotency.Rollback(tx)
    current,err:=renameParent(ctx,tx,id);if err!=nil { return nil,err }
    if current!=name {
        if current!=req.GetExpectedName() { return nil,status.Error(codes.Aborted,"name changed; reload before editing") }
        if _,err=tx.Exec(ctx,`UPDATE models SET name=$2,updated_at=clock_timestamp() WHERE model_id=$1`,id,name);err!=nil { return nil,idempotency.DBError(err) }
    }
    if err=tx.Commit(ctx);err!=nil { return nil,idempotency.DBError(err) }
    return s.GetModel(ctx,&managementv1.GetModelRequest{ModelId:id})
}
func (s *Service) RenameModelVersion(ctx context.Context, req *managementv1.RenameModelVersionRequest) (*managementv1.ModelVersion,error) {
    id,label,err:=renameInput(req.GetModelVersionId(),req.GetVersion(),req.GetExpectedVersion(),128)
    if err!=nil { return nil,err }
    tx,err:=s.pool.Begin(ctx);if err!=nil { return nil,idempotency.DBError(err) };defer idempotency.Rollback(tx)
    var parent,current,state string
    err=tx.QueryRow(ctx,`SELECT model_id FROM model_versions WHERE model_version_id=$1`,id).Scan(&parent)
    if errors.Is(err,pgx.ErrNoRows) { return nil,status.Error(codes.NotFound,"model version not found") }
    if err!=nil { return nil,idempotency.DBError(err) }
    if _,err=renameParent(ctx,tx,parent);err!=nil { return nil,err }
    if err=tx.QueryRow(ctx,`SELECT version,state FROM model_versions WHERE model_version_id=$1 FOR UPDATE`,id).Scan(&current,&state);err!=nil { return nil,idempotency.DBError(err) }
    if state=="deleting" || state=="deleted" { return nil,status.Error(codes.FailedPrecondition,"model version was removed") }
    if current!=label {
        if current!=req.GetExpectedVersion() { return nil,status.Error(codes.Aborted,"version name changed; reload before editing") }
        if _,err=tx.Exec(ctx,`UPDATE model_versions SET version=$2,updated_at=clock_timestamp() WHERE model_version_id=$1`,id,label);err!=nil { return nil,idempotency.DBError(err) }
    }
    if err=tx.Commit(ctx);err!=nil { return nil,idempotency.DBError(err) }
    return s.GetModelVersion(ctx,&managementv1.GetModelVersionRequest{ModelVersionId:id})
}
