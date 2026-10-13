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
func TestEvaluationBodyRejectsSilentOverrides(t *testing.T){
 for _,raw:=range []string{`{"evaluation":{},"evaluation":null}`,`{"evaluation":{"dataset":{"source_type":"version","source_type":"synthetic"}}}`,`{"Evaluation":{}}`,`{"evaluation":{}}`,`{"evaluation":true}`,`{"evaluation":{"dataset":null}}`,`{"evaluation":{"dataset":{"SHA256":"x"}}}`,`{"evaluation":{"dataset":{"source_uri":"s3://x"}}}`,`{"evaluation":{"dataset":{},"max_records":1}}`,`{"evaluation":{"dataset":{"dataset_version_id":3}}}`}{if _,e:=parseEvaluationBody([]byte(raw));e==nil{t.Fatal("invalid evaluation accepted",raw)}}
 out,e:=parseEvaluationBody([]byte(`{"evaluation":{"dataset":{"source_type":"version","dataset_version_id":"id"}}}`));if e!=nil||out.GetDataset().GetDatasetVersionId()!="id"{t.Fatal(out,e)}
 for _,raw:=range []string{`{}`,`{"evaluation":null}`}{out,e:=parseEvaluationBody([]byte(raw));if e!=nil||out!=nil{t.Fatal("legacy request changed")}}
}
type evaluationClient struct{trainingv1.TrainingServiceClient;calls int;comparable bool}
func(c *evaluationClient)CompareTrainingRuns(_ context.Context,r *trainingv1.CompareTrainingRunsRequest,_ ...grpc.CallOption)(*trainingv1.CompareTrainingRunsResponse,error){
 c.calls++;return &trainingv1.CompareTrainingRunsResponse{Comparable:c.comparable,Reason:"fixture",Evaluations:[]*trainingv1.TrainingEvaluation{{TrainingRunId:r.TrainingRunIds[0],State:"ready",Result:&trainingv1.EvaluationResult{Loss:4}},{TrainingRunId:r.TrainingRunIds[1],State:"ready",Result:&trainingv1.EvaluationResult{Loss:3}}}},nil
}
func(c *evaluationClient)GetTrainingEvaluation(_ context.Context,r *trainingv1.GetTrainingRunRequest,_ ...grpc.CallOption)(*trainingv1.TrainingEvaluation,error){c.calls++;return &trainingv1.TrainingEvaluation{TrainingRunId:r.TrainingRunId,State:"ready",Result:&trainingv1.EvaluationResult{Loss:701,PerplexityOverflow:true}},nil}
func TestEvaluationCompareRouteAndConditionalDelta(t *testing.T){
 client:=&evaluationClient{};app:=fiber.New();New(&grpcclient.Clients{Training:client}).Register(app)
 for _,path:=range []string{"/api/training-runs/compare","/api/training-runs/compare?training_run_ids=a","/api/training-runs/compare?training_run_ids=a,b&other=x","/api/training-runs/compare?training_run_ids=a,b&training_run_ids=c,d","/api/training-runs/a/evaluation?tail=1"}{res,e:=app.Test(httptest.NewRequest("GET",path,nil));if e!=nil{t.Fatal(e)};res.Body.Close();if res.StatusCode!=400||client.calls!=0{t.Fatal(path,res.StatusCode)}}
 for _,comparable:=range []bool{false,true}{client.comparable=comparable
  res,e:=app.Test(httptest.NewRequest("GET","/api/training-runs/compare?training_run_ids=a,b",nil));if e!=nil{t.Fatal(e)};var body map[string]any;e=json.NewDecoder(res.Body).Decode(&body);res.Body.Close();if e!=nil||res.StatusCode!=200{t.Fatal(body,e)}
  rows:=body["items"].([]any);delta:=rows[1].(map[string]any)["loss_delta_from_first"]
  if comparable&&delta!=float64(-1)||!comparable&&delta!=nil{t.Fatal("invalid comparison delta",body)}
 }
 res,e:=app.Test(httptest.NewRequest("GET","/api/training-runs/a/evaluation",nil));if e!=nil{t.Fatal(e)};defer res.Body.Close();var body map[string]any;if e=json.NewDecoder(res.Body).Decode(&body);e!=nil{t.Fatal(e)}
 if body["result"].(map[string]any)["perplexity"]!=nil{t.Fatal("overflow became zero")}
 raw,_:=json.Marshal(body);for _,private:=range []string{"dispatch_token","execution_instance_id","optimizer.pt"}{if strings.Contains(string(raw),private){t.Fatal("private data exposed")}}
}
