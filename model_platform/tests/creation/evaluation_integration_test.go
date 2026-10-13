package creation_test

import (
 "context"
 "fmt"
 "reflect"
 "strings"
 "testing"
)
func TestEvaluationCreationPinsBothDatasetsAndReplaysWithoutLookups(t *testing.T){
 f:=newFixture(t);f.trainer.ConfigureDatasets(&datasetMemoryStore{});f.trainer.ConfigureOutputs(t.TempDir(),"",nil,nil);id:=f.createDataset(t)
 code,train:=f.uploadDataset(t,id,"train",datasetContent,"train");if code!=201{t.Fatal(train)}
 code,eval:=f.uploadDataset(t,id,"eval",strings.ReplaceAll(datasetContent,"question","held-out"),"eval");if code!=201{t.Fatal(eval)}
 body:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q},"evaluation":{"dataset":{"source_type":"version","dataset_version_id":%q}}}`,f.versionID,train["dataset_version_id"],eval["dataset_version_id"])
 code,created:=f.request(t,"POST","/api/training-runs",body,"evaluation");if code!=202{t.Fatal(code,created)}
 runID:=created["training_run_id"].(string);code,run:=f.request(t,"GET","/api/training-runs/"+runID,"");if code!=200{t.Fatal(run)}
 pin:=run["evaluation"].(map[string]any)["dataset"].(map[string]any)
 if pin["dataset_version_id"]!=eval["dataset_version_id"]||pin["sha256"]!=eval["sha256"]||pin["source_uri"]!=eval["artifact_uri"]||pin["record_count"]!=eval["record_count"]{t.Fatal("evaluation pin missing",pin)}
 code,pending:=f.request(t,"GET","/api/training-runs/"+runID+"/evaluation","");if code!=200||pending["state"]!="pending"||pending["result"]!=nil{t.Fatal(pending)}
 code,list:=f.request(t,"GET","/api/training-runs?source_model_version_id="+f.versionID,"");if code!=200{t.Fatal(list)}
 items:=list["items"].([]any);if len(items)!=1||!reflect.DeepEqual(items[0].(map[string]any)["evaluation"],run["evaluation"]){t.Fatal("catalog dropped evaluation pin",list)}
 // Replays survive service capability disablement and unavailable dependencies.
 f.managementOffline.Store(true);f.trainer.ConfigureOutputs("","",nil,nil)
 code,replay:=f.request(t,"POST","/api/training-runs",body,"evaluation");if code!=202||!reflect.DeepEqual(created,replay){t.Fatal("replay changed",code,replay)}
 changed:=strings.Replace(body,eval["dataset_version_id"].(string),train["dataset_version_id"].(string),1)
 if code,_:=f.request(t,"POST","/api/training-runs",changed,"evaluation");code!=409{t.Fatal("evaluation change reused request key",code)}
 if count(t,f.training,`SELECT count(*) FROM training_runs`)!=1||count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("replay or lookup created extra work")}
}
func TestEvaluationCreationRejectsSharedFileAndUnsupportedSettings(t *testing.T){
 f:=newFixture(t);f.trainer.ConfigureDatasets(&datasetMemoryStore{});f.trainer.ConfigureOutputs(t.TempDir(),"",nil,nil);id:=f.createDataset(t)
 _,train:=f.uploadDataset(t,id,"train",datasetContent,"train");_,same:=f.uploadDataset(t,id,"same-bytes",datasetContent,"same")
 _,eval:=f.uploadDataset(t,id,"eval",strings.ReplaceAll(datasetContent,"question","held-out"),"eval")
 for name,extra:=range map[string]string{"packing":`,"training":{"sample_packing":true}`,"too-long":`,"training":{"sequence_length":8192}`,"multi-gpu":`,"resources":{"gpu_ids":[0,1]}`} {
  body:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q},"evaluation":{"dataset":{"source_type":"version","dataset_version_id":%q}}%s}`,f.versionID,train["dataset_version_id"],eval["dataset_version_id"],extra)
  if code,result:=f.request(t,"POST","/api/training-runs",body,name);code!=400{t.Fatal(name,code,result)}
 }
 for _,v:=range []map[string]any{train,same}{body:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q},"evaluation":{"dataset":{"source_type":"version","dataset_version_id":%q}}}`,f.versionID,train["dataset_version_id"],v["dataset_version_id"])
  if code,_:=f.request(t,"POST","/api/training-runs",body,"shared-file");code!=400{t.Fatal("same file accepted",code)}
 }
 if count(t,f.training,`SELECT count(*) FROM training_runs`)!=0||count(t,f.ops,`SELECT count(*) FROM operations`)!=0{t.Fatal("invalid evaluation scheduled work")}
 // A legacy row retains its disabled evaluation after the schema migration.
 _,e:=f.training.Exec(context.Background(),`INSERT INTO training_runs(training_run_id,source_model_version_id,status) VALUES(gen_random_uuid(),$1,'created')`,f.versionID);if e!=nil{t.Fatal(e)}
}
