package creation_test

import (
 "context"
 "encoding/json"
 "fmt"
 "strings"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/trainingparallel"
 "github.com/google/uuid"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
func TestMultiGPUTrainingContractAndReplay(t *testing.T){
 f:=newFixture(t)
 body:=fmt.Sprintf(`{"source_model_version_id":%q,"resources":{"gpu_ids":[3,1]}}`,f.versionID)
 code,first:=f.request(t,"POST","/api/training-runs",body,"ddp-create")
 if code!=202{t.Fatalf("DDP start: %d %v",code,first)}
 code,run:=f.request(t,"GET","/api/training-runs/"+first["training_run_id"].(string),"")
 if code!=200 || run["training"].(map[string]any)["strategy"]!="ddp"{t.Fatalf("DDP not persisted: %v",run)}
 resources:=run["resources"].(map[string]any);ids:=resources["gpu_ids"].([]any)
 if resources["gpu_count"]!=float64(2)||ids[0]!=float64(1)||ids[1]!=float64(3){t.Fatal(resources)}
 replayBody:=fmt.Sprintf(`{"source_model_version_id":%q,"resources":{"gpu_ids":[1,3]},"training":{"strategy":"ddp"}}`,f.versionID)
 code,replay:=f.request(t,"POST","/api/training-runs",replayBody,"ddp-create")
 if code!=202||replay["training_run_id"]!=first["training_run_id"]||replay["operation_id"]!=first["operation_id"]{t.Fatal("normalized DDP replay changed identity")}
}
func TestMultiGPUInvalidRequestsNeverCreateRun(t *testing.T){
 f:=newFixture(t)
 for _,fragment:=range []string{
  `"resources":{"gpu_count":2}`,
  `"resources":{"gpu_ids":[0,1]},"training":{"strategy":"single"}`,
  `"resources":{"gpu_ids":[0]},"training":{"strategy":"ddp"}`,
  `"resources":{"gpu_ids":[0,1]},"training":{"strategy":"fsdp"}`,
  `"resources":{"gpu_ids":[0,0]}`,
 }{
  body:=fmt.Sprintf(`{"source_model_version_id":%q,%s}`,f.versionID,fragment)
  if code,res:=f.request(t,"POST","/api/training-runs",body);code!=400{t.Fatalf("invalid input: %d %v",code,res)}
 }
 if count(t,f.training,`SELECT count(*) FROM training_runs`)!=0{t.Fatal("invalid GPU request created work")}
}

func TestMultiGPUCompletionRequiresEveryRankAndPreservesReplay(t *testing.T){
 f:=newFixture(t)
 body:=fmt.Sprintf(`{"source_model_version_id":%q,"resources":{"gpu_ids":[0,1]}}`,f.versionID)
 code,created:=f.request(t,"POST","/api/training-runs",body,"complete-ddp");if code!=202{t.Fatal(created)}
 runID,outputID:=created["training_run_id"].(string),uuid.NewString()
 uri,hash:="s3://test/output/",strings.Repeat("a",64)
 execSQL(t,f.management,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind,training_run_id,source_model_version_id,artifact_uri,artifact_sha256,artifact_size_bytes,artifact_manifest_sha256,artifact_file_count) VALUES($1,$2,'output','ready','training','adapter',$3,$4,$5,$6,10,$6,2)`,outputID,f.modelID,runID,f.versionID,uri,hash)
 result:=map[string]any{"train_exit_code":0,"output_file_count":2,"output_model_version_id":outputID,"output_artifact_uri":uri,"output_manifest_sha256":hash}
 complete:=func()error{
  raw,err:=json.Marshal(result);if err!=nil{t.Fatal(err)}
  _,err=f.clients.Training.CompleteTraining(context.Background(),&trainingv1.CompleteTrainingRequest{TrainingRunId:runID,OutputModelVersionId:outputID,ResultJson:string(raw)})
  return err
 }
 if err:=complete();status.Code(err)!=codes.FailedPrecondition{t.Fatalf("missing DDP evidence accepted: %v",err)}
 parallel:=trainingparallel.Result{Strategy:"ddp",WorldSize:2,EffectiveBatchSize:2}
 for i:=0;i<2;i++ {parallel.Ranks=append(parallel.Ranks,trainingparallel.Rank{Rank:i,LocalRank:i,WorldSize:2,TrainingRunID:runID,DeviceUUID:fmt.Sprintf("GPU-%d",i),Initialized:true,DDPWrapped:true,Completed:true,GlobalStep:1})}
 partial:=parallel;partial.Ranks=parallel.Ranks[:1];result["parallelism"]=partial
 if err:=complete();status.Code(err)!=codes.FailedPrecondition{t.Fatalf("partial DDP result accepted: %v",err)}
 if count(t,f.training,`SELECT count(*) FROM training_runs WHERE status='succeeded'`)!=0{t.Fatal("partial result changed state")}
 result["parallelism"]=parallel
 if err:=complete();err!=nil{t.Fatal(err)}
 if err:=complete();err!=nil{t.Fatalf("completion replay failed: %v",err)}
}
