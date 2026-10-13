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
)
type outputClient struct{trainingv1.TrainingServiceClient;state string;calls int;key string}
func(c *outputClient)GetTrainingOutput(_ context.Context,r *trainingv1.GetTrainingRunRequest,_ ...grpc.CallOption)(*trainingv1.TrainingOutput,error){c.calls++;return &trainingv1.TrainingOutput{TrainingRunId:r.TrainingRunId,OperationId:"original-operation",RecoveryId:"recovery",RecoveryStatus:c.state,Availability:"retained",OutputModelVersionId:"recovered-adapter"},nil}
func(c *outputClient)RetryTrainingOutput(ctx context.Context,r *trainingv1.RetryTrainingOutputRequest,_ ...grpc.CallOption)(*trainingv1.TrainingOutput,error){c.key=r.IdempotencyKey;return c.GetTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId})}
func TestOutputRESTReplayStatusAndPublicFields(t *testing.T){
 client:=&outputClient{};app:=fiber.New();New(&grpcclient.Clients{Training:client}).Register(app)
 for _,state:=range []string{"queued","running","failed","succeeded"}{client.state=state;req:=httptest.NewRequest("POST","/api/training-runs/id/output/retry",nil);req.Header.Set("Idempotency-Key","retry-key")
  res,e:=app.Test(req);if e!=nil{t.Fatal(e)};var body map[string]any;e=json.NewDecoder(res.Body).Decode(&body);res.Body.Close();code:=200;if state=="queued"||state=="running"{code=202}
  if e!=nil||res.StatusCode!=code||body["recovery_status"]!=state||body["output_model_version_id"]!="recovered-adapter"||client.key!="retry-key"{t.Fatal(res.StatusCode,body,e)}
  for _,key:=range []string{"dispatch_token","execution_instance_id","result","result_json","idempotency_key","output_dir"}{if _,ok:=body[key];ok{t.Fatal("private output field exposed",key)}}
 }
}
func TestOutputRESTRejectsUnsupportedInput(t *testing.T){
 app:=fiber.New();New(&grpcclient.Clients{}).Register(app)
 for _,item:=range []struct{method,path,body,key string}{
  {"POST","/api/training-runs/id/output/retry","",""},
  {"POST","/api/training-runs/id/output/retry","{}","key"},
  {"POST","/api/training-runs/id/output/retry?force=true","","key"},
  {"GET","/api/training-runs/id/output?path=/other","",""},
  {"POST","/api/training-runs/id/output/release","{\"force\":true}",""},
 }{req:=httptest.NewRequest(item.method,item.path,strings.NewReader(item.body));if item.key!=""{req.Header.Set("Idempotency-Key",item.key)};res,e:=app.Test(req);if e!=nil{t.Fatal(e)};res.Body.Close();if res.StatusCode!=400{t.Fatal(item,res.StatusCode)}}
}
