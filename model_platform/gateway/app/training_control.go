package app

import (
 "context"
 "net/url"
 "strconv"
 "time"
 trainingv1 "model-platform/gen/training/v1"
 "github.com/gofiber/fiber/v3"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

func(a *API)cancelTraining(c fiber.Ctx)error{
 // Cancellation is idempotent by resource ID; no body or new Operation exists.
 if len(c.Body())!=0||len(c.Request().URI().QueryString())!=0{return c.Status(400).JSON(fiber.Map{"error":"cancel does not accept a body or query"})}
 ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
 run,err:=a.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:c.Params("id")});if err!=nil{return grpcError(c,err)}
 code:=200;if run.GetStatus()=="cancelling"{code=202}
 return c.Status(code).JSON(trainingDetailMap(run))
}
func(a *API)getTrainingLogs(c fiber.Ctx)error{
 query,err:=url.ParseQuery(string(c.Request().URI().QueryString()));if err!=nil{return grpcError(c,status.Error(codes.InvalidArgument,"invalid log query"))}
 values:=map[string]int32{"tail_lines":200,"max_bytes":65536}
 for key,raw:=range query{
  if _,ok:=values[key];!ok||len(raw)!=1{return grpcError(c,status.Error(codes.InvalidArgument,"unknown or duplicate log query"))}
  n,err:=strconv.ParseInt(raw[0],10,32);if err!=nil||n<1{return grpcError(c,status.Error(codes.InvalidArgument,"invalid log limit"))};values[key]=int32(n)
 }
 if values["tail_lines"]>2000||values["max_bytes"]<1024||values["max_bytes"]>262144{return grpcError(c,status.Error(codes.InvalidArgument,"log limits out of range"))}
 ctx,cancel:=context.WithTimeout(c.Context(),30*time.Second);defer cancel()
 logs,err:=a.clients.Training.GetTrainingLogs(ctx,&trainingv1.GetTrainingLogsRequest{TrainingRunId:c.Params("id"),TailLines:values["tail_lines"],MaxBytes:values["max_bytes"]});if err!=nil{return grpcError(c,err)}
 c.Set("Cache-Control","no-store")
 return c.JSON(fiber.Map{"training_run_id":logs.GetTrainingRunId(),"operation_id":logs.GetOperationId(),"source":logs.GetSource(),"availability":logs.GetAvailability(),"stdout":logs.GetStdout(),"stderr":logs.GetStderr(),"truncated":logs.GetTruncated(),"tail_lines":logs.GetTailLines(),"max_bytes":logs.GetMaxBytes()})
}
