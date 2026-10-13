package app

import (
    "context"
    "encoding/json"
    "path"
    "strings"
    "unicode"

    operationv1 "knowledge-platform/gen/operation/v1"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

func validateTransferProgress(p *operationv1.TransferProgress) error {
    name := p.GetCurrentFile()
    if p.GetSequence() < 1 || len(name) == 0 || len(name) > 1024 || path.IsAbs(name) || path.Clean(name) != name || name == ".." || strings.HasPrefix(name, "../") || strings.Contains(name, "\\") || strings.IndexFunc(name, unicode.IsControl) >= 0 {
        return status.Error(codes.InvalidArgument, "invalid transfer file or sequence")
    }
    if p.GetStage() != "downloading" && p.GetStage() != "uploading" && p.GetStage() != "stored" {
        return status.Error(codes.InvalidArgument, "invalid transfer stage")
    }
    if p.GetFilesTotal() < 1 || p.GetFilesCompleted() < 0 || p.GetFilesCompleted() > p.GetFilesTotal() || p.GetBytesCompleted() < 0 || p.GetBytesTotal() < 0 || (p.GetBytesTotal() > 0 && p.GetBytesCompleted() > p.GetBytesTotal()) {
        return status.Error(codes.InvalidArgument, "invalid transfer counters")
    }
    return nil
}

// Uses the same execution identity and row lock as lifecycle reports. Progress
// never changes terminal state or rearms a Job, and old timed-out reports cannot
// overwrite a newer sample. Only the latest sample is retained.
func (s *Service) updateTransferProgress(ctx context.Context, req *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
    if _, err := uuid.Parse(req.GetOperationId()); err != nil { return nil, status.Error(codes.InvalidArgument, "valid operation_id is required") }
    if req.GetPhase() != "transferring_files" { return nil, status.Error(codes.InvalidArgument, "invalid transfer phase") }
    if err := validateTransferProgress(req.GetProgress()); err != nil { return nil, err }
    tx, err := s.pool.Begin(ctx)
    if err != nil { return nil, status.Error(codes.Internal, "begin progress update") }
    defer tx.Rollback(ctx)
    op, err := lockOperation(ctx, tx, req.GetOperationId())
    if err != nil { return nil, err }
    if op.GetExecutionType() != "management.import-huggingface" || op.GetStatus() != "running" || op.GetPhase() == "writing_manifest" || op.GetDispatchProtocol() != 1 || op.GetExecutionStartedAt() == nil || req.GetDispatchToken() != op.GetDispatchToken() || req.GetExecutionInstanceId() != op.GetExecutionInstanceId() {
        return nil, status.Error(codes.FailedPrecondition, "progress is not from the active authorized import")
    }
    if req.GetProgress().GetSequence() <= op.GetProgress().GetSequence() { return op, nil }
    raw, err := json.Marshal(req.GetProgress())
    if err != nil { return nil, status.Error(codes.InvalidArgument, "invalid progress") }
    _, err = tx.Exec(ctx, `UPDATE operations SET phase='transferring_files',progress_json=$2::jsonb,progress_updated_at=clock_timestamp(),updated_at=clock_timestamp() WHERE operation_id=$1`, req.GetOperationId(), raw)
    if err != nil { return nil, status.Error(codes.Internal, "write progress") }
    return commitOperation(ctx, tx, req.GetOperationId())
}
