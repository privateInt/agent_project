package app

import (
 "bytes"
 "context"
 "encoding/json"
 "io"
 "time"
 trainingv1 "model-platform/gen/training/v1"
 "github.com/gofiber/fiber/v3"
)
func resumeMap(r *trainingv1.TrainingResume)any{if r==nil{return nil};return fiber.Map{"source_training_run_id":r.GetSourceTrainingRunId(),"checkpoint_step":r.GetCheckpointStep(),"checkpoint_sha256":r.GetCheckpointSha256()}}
func(a *API)listTrainingCheckpoints(c fiber.Ctx)error{
 if !emptyOutputRequest(c){return c.Status(400).JSON(fiber.Map{"error":"checkpoint lookup does not accept body or query"})}
 ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
 out,e:=a.clients.Training.ListTrainingCheckpoints(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:c.Params("id")});if e!=nil{return grpcError(c,e)}
 items:=[]fiber.Map{};for _,r:=range out.GetCheckpoints(){items=append(items,fiber.Map{"step":r.GetStep(),"max_steps":r.GetMaxSteps(),"checkpoint_sha256":r.GetCheckpointSha256(),"size_bytes":r.GetSizeBytes(),"file_count":r.GetFileCount(),"resumable":r.GetResumable()})}
 return c.JSON(fiber.Map{"training_run_id":out.GetTrainingRunId(),"availability":out.GetAvailability(),"checkpoints":items})
}
func(a *API)resumeTraining(c fiber.Ctx)error{
 if len(c.Request().URI().QueryString())!=0{return c.Status(400).JSON(fiber.Map{"error":"resume does not accept query parameters"})}
 key,e:=requestKey(c);if e!=nil{return grpcError(c,e)};if key==""{return c.Status(400).JSON(fiber.Map{"error":"Idempotency-Key is required"})}
 // Exact key matching prevents silently ignored overrides (including casing).
 var fields map[string]json.RawMessage
 dec:=json.NewDecoder(bytes.NewReader(c.Body()));var extra any
 if dec.Decode(&fields)!=nil||dec.Decode(&extra)!=io.EOF||len(fields)!=1{return c.Status(400).JSON(fiber.Map{"error":"resume accepts only checkpoint_step"})}
 var step int32
 if raw,ok:=fields["checkpoint_step"];!ok||json.Unmarshal(raw,&step)!=nil||step<=0{return c.Status(400).JSON(fiber.Map{"error":"checkpoint_step must be a positive integer"})}
 ctx,cancel:=context.WithTimeout(c.Context(),20*time.Second);defer cancel()
 out,e:=a.clients.Training.ResumeTraining(ctx,&trainingv1.ResumeTrainingRequest{TrainingRunId:c.Params("id"),CheckpointStep:step,IdempotencyKey:key});if e!=nil{return grpcError(c,e)}
 return c.Status(202).JSON(fiber.Map{"training_run_id":out.GetTrainingRunId(),"operation_id":out.GetOperationId(),"status":out.GetStatus()})
}
