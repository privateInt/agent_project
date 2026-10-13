package app

import (
 "context"
 "encoding/json"
 "net/http/httptest"
 "strings"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/gateway/grpcclient"
 "github.com/gofiber/fiber/v3"
 "google.golang.org/grpc"
 "google.golang.org/protobuf/types/known/timestamppb"
)
type trainingControlClient struct{trainingv1.TrainingServiceClient;state string;logs *trainingv1.GetTrainingLogsRequest}
func(c *trainingControlClient)CancelTraining(_ context.Context,r *trainingv1.GetTrainingRunRequest,_ ...grpc.CallOption)(*trainingv1.TrainingRun,error){return &trainingv1.TrainingRun{TrainingRunId:r.TrainingRunId,Status:c.state,CancelRequestedAt:timestamppb.Now()},nil}
func(c *trainingControlClient)GetTrainingLogs(_ context.Context,r *trainingv1.GetTrainingLogsRequest,_ ...grpc.CallOption)(*trainingv1.TrainingLogs,error){c.logs=r;return &trainingv1.TrainingLogs{TrainingRunId:r.TrainingRunId,Source:"docker",Availability:"available",Stdout:"hello",TailLines:r.TailLines,MaxBytes:r.MaxBytes},nil}
func TestTrainingControlRESTStatusAndLimits(t *testing.T){
 client:=&trainingControlClient{};app:=fiber.New();New(&grpcclient.Clients{Training:client}).Register(app)
 for _,state:=range []string{"cancelling","cancelled","succeeded","failed"}{
  client.state=state;res,err:=app.Test(httptest.NewRequest("POST","/api/training-runs/id/cancel",nil));if err!=nil{t.Fatal(err)}
  var body map[string]any;err=json.NewDecoder(res.Body).Decode(&body);res.Body.Close();code:=200;if state=="cancelling"{code=202}
  if err!=nil||res.StatusCode!=code||body["status"]!=state||body["cancel_requested_at"]==nil{t.Fatal(res.StatusCode,body,err)}
 }
 res,err:=app.Test(httptest.NewRequest("GET","/api/training-runs/id/logs?tail_lines=20&max_bytes=2048",nil));if err!=nil{t.Fatal(err)};defer res.Body.Close()
 if res.StatusCode!=200||client.logs.GetTailLines()!=20||client.logs.GetMaxBytes()!=2048||res.Header.Get("Cache-Control")!="no-store"{t.Fatal("log options not forwarded")}
}
func TestTrainingControlRejectsAmbiguousInputBeforeRPC(t *testing.T){
 app:=fiber.New();New(&grpcclient.Clients{}).Register(app)
 for _,query:=range []string{"tail_lines=0","tail_lines=2001","max_bytes=1023","max_bytes=262145","tail_lines=x","tail_lines=1&tail_lines=2","container_id=foreign","follow=true"}{
  res,err:=app.Test(httptest.NewRequest("GET","/api/training-runs/id/logs?"+query,nil));if err!=nil{t.Fatal(err)};res.Body.Close();if res.StatusCode!=400{t.Fatal(query,res.StatusCode)}
 }
 for _,body:=range []string{"{}","{\"force\":true}"}{res,err:=app.Test(httptest.NewRequest("POST","/api/training-runs/id/cancel",strings.NewReader(body)));if err!=nil{t.Fatal(err)};res.Body.Close();if res.StatusCode!=400{t.Fatal(res.StatusCode)}}
}
