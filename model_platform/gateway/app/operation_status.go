package app

import (
 "time"
 operationv1 "model-platform/gen/operation/v1"
 "github.com/gofiber/fiber/v3"
 "google.golang.org/protobuf/types/known/timestamppb"
)
func optionalTime(t *timestamppb.Timestamp)any{if t==nil{return nil};return t.AsTime().UTC().Format(time.RFC3339Nano)}
func operationMap(r *operationv1.Operation)fiber.Map{
 return fiber.Map{"operation_id":r.GetOperationId(),"kind":r.GetKind(),"execution_type":r.GetExecutionType(),"resource_type":r.GetResourceType(),"resource_id":r.GetResourceId(),"status":r.GetStatus(),"phase":r.GetPhase(),"attempt":r.GetAttempt(),"max_attempts":r.GetMaxAttempts(),"error_code":r.GetErrorCode(),"error_message":r.GetErrorMessage(),"retryable":r.GetRetryable(),"resources":resourcesMap(r.GetResources()),
 "progress":transferProgressMap(r.GetProgress()),"progress_updated_at":optionalTime(r.GetProgressUpdatedAt()),
 "dispatch_generation":r.GetDispatchGeneration(),"gpu_wait_count":r.GetGpuWaitCount(),"gpu_wait_until":optionalTime(r.GetGpuWaitUntil()),"dispatch_lease_until":optionalTime(r.GetDispatchLeaseUntil()),"execution_started_at":optionalTime(r.GetExecutionStartedAt())}
}

func transferProgressMap(p *operationv1.TransferProgress) any {
 if p == nil { return nil }
 return fiber.Map{"stage":p.GetStage(),"current_file":p.GetCurrentFile(),"bytes_completed":p.GetBytesCompleted(),"bytes_total":p.GetBytesTotal(),"files_completed":p.GetFilesCompleted(),"files_total":p.GetFilesTotal(),"sequence":p.GetSequence()}
}
