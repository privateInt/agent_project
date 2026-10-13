package app

import (
 "context"
 "time"
 trainingv1 "model-platform/gen/training/v1"
 "github.com/gofiber/fiber/v3"
)

func outputJSON(o *trainingv1.TrainingOutput)fiber.Map{
 return fiber.Map{"training_run_id":o.GetTrainingRunId(),"operation_id":o.GetOperationId(),"availability":o.GetAvailability(),"manifest_sha256":o.GetManifestSha256(),"file_count":o.GetFileCount(),"size_bytes":o.GetSizeBytes(),"recovery_id":o.GetRecoveryId(),"recovery_status":o.GetRecoveryStatus(),"error_code":o.GetErrorCode(),"output_model_version_id":o.GetOutputModelVersionId(),"requested_at":optionalTime(o.GetRequestedAt()),"updated_at":optionalTime(o.GetUpdatedAt())}
}
func emptyOutputRequest(c fiber.Ctx)bool{return len(c.Body())==0&&len(c.Request().URI().QueryString())==0}
func(a *API)getTrainingOutput(c fiber.Ctx)error{
 if !emptyOutputRequest(c){return c.Status(400).JSON(fiber.Map{"error":"output lookup does not accept body or query"})}
 ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
 out,e:=a.clients.Training.GetTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:c.Params("id")});if e!=nil{return grpcError(c,e)};return c.JSON(outputJSON(out))
}
func(a *API)retryTrainingOutput(c fiber.Ctx)error{
 if !emptyOutputRequest(c){return c.Status(400).JSON(fiber.Map{"error":"output retry does not accept body or query"})}
 key,e:=requestKey(c);if e!=nil{return grpcError(c,e)};if key==""{return c.Status(400).JSON(fiber.Map{"error":"Idempotency-Key is required"})}
 ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
 out,e:=a.clients.Training.RetryTrainingOutput(ctx,&trainingv1.RetryTrainingOutputRequest{TrainingRunId:c.Params("id"),IdempotencyKey:key});if e!=nil{return grpcError(c,e)}
 code:=200;if out.GetRecoveryStatus()=="queued"||out.GetRecoveryStatus()=="running"{code=202};return c.Status(code).JSON(outputJSON(out))
}
func(a *API)releaseTrainingOutput(c fiber.Ctx)error{
 if !emptyOutputRequest(c){return c.Status(400).JSON(fiber.Map{"error":"output release does not accept body or query"})}
 // Remote bytes are verified before local deletion. This can take minutes for
 // large saved embeddings; a deadline leaves files for a subsequent request.
 ctx,cancel:=context.WithTimeout(c.Context(),5*time.Minute);defer cancel()
 out,e:=a.clients.Training.ReleaseTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:c.Params("id")});if e!=nil{return grpcError(c,e)};return c.JSON(outputJSON(out))
}
