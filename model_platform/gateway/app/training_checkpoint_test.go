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
type checkpointClient struct{trainingv1.TrainingServiceClient;calls int;key string}
func(c *checkpointClient)ResumeTraining(_ context.Context,r *trainingv1.ResumeTrainingRequest,_ ...grpc.CallOption)(*trainingv1.StartTrainingResponse,error){c.calls++;c.key=r.IdempotencyKey;return &trainingv1.StartTrainingResponse{TrainingRunId:"child",OperationId:"new-operation",Status:"pending"},nil}
func(c *checkpointClient)ListTrainingCheckpoints(_ context.Context,r *trainingv1.GetTrainingRunRequest,_ ...grpc.CallOption)(*trainingv1.TrainingCheckpoints,error){return &trainingv1.TrainingCheckpoints{TrainingRunId:r.TrainingRunId,Availability:"retained",Checkpoints:[]*trainingv1.TrainingCheckpoint{{Step:2,MaxSteps:4,Resumable:true,CheckpointSha256:"hash"}}},nil}
func TestCheckpointRESTRejectsOverridesAndAcceptsExactSelection(t *testing.T){
 client:=&checkpointClient{};app:=fiber.New();New(&grpcclient.Clients{Training:client}).Register(app)
 for _,body:=range []string{`{}`,`null`,`{"checkpoint_step":0}`,`{"checkpoint_step":2.5}`,`{"Checkpoint_Step":2}`,`{"checkpoint_step":2,"max_steps":8}`,`{"checkpoint_step":2,"path":"/other"}`,`{"checkpoint_step":2} {}`}{req:=httptest.NewRequest("POST","/api/training-runs/parent/resume",strings.NewReader(body));req.Header.Set("Idempotency-Key","key");res,e:=app.Test(req);if e!=nil{t.Fatal(e)};res.Body.Close();if res.StatusCode!=400||client.calls!=0{t.Fatal(body,res.StatusCode)}}
 req:=httptest.NewRequest("POST","/api/training-runs/parent/resume",strings.NewReader(`{"checkpoint_step":2}`));req.Header.Set("Idempotency-Key","key");res,e:=app.Test(req);if e!=nil{t.Fatal(e)};defer res.Body.Close();var body map[string]any;_ = json.NewDecoder(res.Body).Decode(&body)
 if res.StatusCode!=202||body["training_run_id"]!="child"||body["operation_id"]!="new-operation"||client.key!="key"{t.Fatal(body,res.StatusCode)}
}
func TestCheckpointRESTSummaryHidesExecutionSecrets(t *testing.T){
 app:=fiber.New();New(&grpcclient.Clients{Training:&checkpointClient{}}).Register(app)
 res,e:=app.Test(httptest.NewRequest("GET","/api/training-runs/parent/checkpoints",nil));if e!=nil{t.Fatal(e)};defer res.Body.Close();var body map[string]any;if e=json.NewDecoder(res.Body).Decode(&body);e!=nil{t.Fatal(e)}
 raw,_:=json.Marshal(body);for _,private:=range []string{"dispatch_token","execution_instance_id","receipt","data_path","optimizer.pt"}{if strings.Contains(string(raw),private){t.Fatal("private checkpoint data exposed",private)}}
 if res.StatusCode!=200||body["availability"]!="retained"||len(body["checkpoints"].([]any))!=1{t.Fatal(body)}
}
