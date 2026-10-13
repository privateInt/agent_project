package app

import (
 "context"
 "encoding/json"
 "math"
 "os"
 "path/filepath"
 "strings"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/trainingevaluation"
 "model-platform/internal/trainingoutput"
 "github.com/google/uuid"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
)
func evalSpecFixture()*trainingv1.EvaluationSpec{return &trainingv1.EvaluationSpec{Dataset:&trainingv1.DatasetSpec{SourceType:"version",Format:"chat_messages",DatasetVersionId:uuid.NewString(),SourceUri:"s3://test/eval.jsonl",Sha256:strings.Repeat("b",64),SizeBytes:200,RecordCount:2}}}
func setEvaluationFixture(t *testing.T,f *outputFixture)(*trainingv1.EvaluationSpec,trainingevaluation.Result){
 t.Helper();spec:=evalSpecFixture();training,_:=NormalizeTrainingSpec(&trainingv1.TrainingSpec{MaxSteps:2,SequenceLength:256});specJSON,_:=json.Marshal(spec);trainingJSON,_:=json.Marshal(training)
 if _,e:=f.s.pool.Exec(context.Background(),`UPDATE training_runs SET evaluation_spec=$2::jsonb,training_spec=$3::jsonb WHERE training_run_id=$1`,f.r.RunID,specJSON,trainingJSON);e!=nil{t.Fatal(e)}
 p:=math.Exp(2);r:=trainingevaluation.Result{Protocol:trainingevaluation.Protocol,CompatibilitySHA256:strings.Repeat("a",64),DatasetVersionID:spec.Dataset.DatasetVersionId,DatasetSHA256:spec.Dataset.Sha256,RecordCount:2,ScoredTokens:4,SequenceLength:256,TrainedSteps:2,NLLSum:8,Loss:2,Perplexity:&p,TokenizedSHA256:strings.Repeat("c",64),RunID:f.r.RunID,SourceID:f.r.SourceID}
 return spec,r
}
func TestEvaluationStateRecoveryAndImmutableExecution(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();req:=&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID}
 out,e:=f.s.GetTrainingEvaluation(ctx,req);if e!=nil||out.State!="disabled"{t.Fatal(out,e)}
 spec,r:=setEvaluationFixture(t,f)
 out,e=f.s.GetTrainingEvaluation(ctx,req);if e!=nil||out.State!="unavailable"||out.Result!=nil{t.Fatal(out,e)}
 // Add valid evaluation to the independent fixture's completed receipt.
 var result map[string]any;_ = json.Unmarshal(f.r.Result,&result);result["evaluation"]=r;f.r.Result,_=json.Marshal(result)
 if e=os.Remove(filepath.Join(f.dir,trainingoutput.ReceiptName));e!=nil{t.Fatal(e)}
 if e=trainingoutput.Seal(f.dir,f.r);e!=nil{t.Fatal(e)}
 parent,e:=f.s.get(ctx,f.r.RunID);if e!=nil{t.Fatal(e)}
 f.retry(t,"evaluation-output");f.process(t)
 out,e=f.s.GetTrainingEvaluation(ctx,req);if e!=nil||out.State!="ready"||out.ResultOrigin!="output_recovery"||out.OutputModelVersionId!=f.mgmt.mv.ModelVersionId||!proto.Equal(out.Evaluation,spec)||out.Result.GetLoss()!=2{t.Fatal(out,e)}
 after,e:=f.s.get(ctx,f.r.RunID);if e!=nil||after.Status!=parent.Status||after.ResultJson!=parent.ResultJson{t.Fatal("evaluation read rewrote parent",after,e)}
 // Release of locally retained output cannot erase the committed evaluation.
 if _,e=f.s.ReleaseTrainingOutput(ctx,req);e!=nil{t.Fatal(e)}
 if after,e:=f.s.GetTrainingEvaluation(ctx,req);e!=nil||!proto.Equal(out,after){t.Fatal("release changed evaluation",after,e)}
 f.mgmt.mv.ArtifactManifestSha256="changed"
 if _,e=f.s.GetTrainingEvaluation(ctx,req);status.Code(e)!=codes.FailedPrecondition{t.Fatal("mismatched output served as ready evaluation",e)}
}
func TestEvaluationMissingEvidenceBlocksCompletionAndRecovery(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();setEvaluationFixture(t,f)
 if _,e:=f.s.RetryTrainingOutput(ctx,&trainingv1.RetryTrainingOutputRequest{TrainingRunId:f.r.RunID,IdempotencyKey:"missing-evaluation"});status.Code(e)!=codes.FailedPrecondition{t.Fatal("missing evaluation recovery accepted",e)}
 f.mgmt.mv.State="ready";f.mgmt.mv.ArtifactUri="s3://test/output/";f.mgmt.mv.ArtifactFileCount=2
 result:=map[string]any{"train_exit_code":0,"output_file_count":2,"output_model_version_id":f.mgmt.mv.ModelVersionId,"output_artifact_uri":f.mgmt.mv.ArtifactUri,"output_manifest_sha256":f.r.Digest}
 raw,_:=json.Marshal(result)
 if _,e:=f.s.CompleteTraining(ctx,&trainingv1.CompleteTrainingRequest{TrainingRunId:f.r.RunID,OutputModelVersionId:f.mgmt.mv.ModelVersionId,ResultJson:string(raw)});status.Code(e)!=codes.FailedPrecondition||!strings.Contains(e.Error(),"evaluation"){t.Fatal("completion did not require evaluation",e)}
 var current string;if e:=f.s.pool.QueryRow(ctx,`SELECT status FROM training_runs WHERE training_run_id=$1`,f.r.RunID).Scan(&current);e!=nil||current!="failed"{t.Fatal(current,e)}
}
func TestCheckpointResumeCopiesEvaluationPinAndRejectsReceiptMismatch(t *testing.T){
 for _,mismatch:=range []bool{false,true}{t.Run(map[bool]string{false:"pinned",true:"changed"}[mismatch],func(t *testing.T){
  f,r:=checkpointFixture(t);ctx:=context.Background();spec:=evalSpecFixture();raw,_:=json.Marshal(spec)
  if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET evaluation_spec=$2::jsonb WHERE training_run_id=$1`,f.r.RunID,raw);e!=nil{t.Fatal(e)}
  r.Evaluation=raw;if mismatch{other:=proto.Clone(spec).(*trainingv1.EvaluationSpec);other.Dataset.DatasetVersionId=uuid.NewString();r.Evaluation,_=json.Marshal(other)}
  saveCheckpointFixture(t,f,r)
  out,e:=f.s.ResumeTraining(ctx,&trainingv1.ResumeTrainingRequest{TrainingRunId:f.r.RunID,CheckpointStep:2,IdempotencyKey:"eval-resume"})
  if mismatch{if status.Code(e)!=codes.FailedPrecondition{t.Fatal("changed eval pin resumed",e)};return}
  if e!=nil{t.Fatal(e)};child,e:=f.s.get(ctx,out.TrainingRunId);if e!=nil||!proto.Equal(child.Evaluation,spec){t.Fatal("resume dropped evaluation",child,e)}
 })}
}
func TestCompareRejectsDuplicateIDsAndReportsDisabledLegacyRuns(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background()
 if _,e:=f.s.CompareTrainingRuns(ctx,&trainingv1.CompareTrainingRunsRequest{TrainingRunIds:[]string{f.r.RunID,f.r.RunID}});status.Code(e)!=codes.InvalidArgument{t.Fatal(e)}
 second:=uuid.NewString();if _,e:=f.s.pool.Exec(ctx,`INSERT INTO training_runs(training_run_id,source_model_version_id,status) VALUES($1,$2,'created')`,second,f.r.SourceID);e!=nil{t.Fatal(e)}
 out,e:=f.s.CompareTrainingRuns(ctx,&trainingv1.CompareTrainingRunsRequest{TrainingRunIds:[]string{f.r.RunID,second}})
 if e!=nil||out.Comparable||out.Reason!="evaluation_not_ready"||len(out.Evaluations)!=2||out.Evaluations[0].State!="disabled"{t.Fatal(out,e)}
}
func TestEvaluationCompletionCommitsValidatedMetrics(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();_,r:=setEvaluationFixture(t,f)
 if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET status='running' WHERE training_run_id=$1`,f.r.RunID);e!=nil{t.Fatal(e)}
 f.mgmt.mv.State="ready";f.mgmt.mv.ArtifactUri="s3://test/output/";f.mgmt.mv.ArtifactFileCount=2
 result:=map[string]any{"train_exit_code":0,"output_file_count":2,"output_model_version_id":f.mgmt.mv.ModelVersionId,"output_artifact_uri":f.mgmt.mv.ArtifactUri,"output_manifest_sha256":f.r.Digest}
 bad:=r;bad.RecordCount=1;result["evaluation"]=bad;raw,_:=json.Marshal(result)
 request:=&trainingv1.CompleteTrainingRequest{TrainingRunId:f.r.RunID,OutputModelVersionId:f.mgmt.mv.ModelVersionId,ResultJson:string(raw)}
 if _,e:=f.s.CompleteTraining(ctx,request);status.Code(e)!=codes.FailedPrecondition{t.Fatal("partial evaluation completed",e)}
 result["evaluation"]=r;raw,_=json.Marshal(result);request.ResultJson=string(raw)
 out,e:=f.s.CompleteTraining(ctx,request);if e!=nil||out.Status!="succeeded"{t.Fatal(out,e)}
 view,e:=f.s.GetTrainingEvaluation(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil||view.State!="ready"||view.ResultOrigin!="training"||view.Result.Loss!=r.Loss{t.Fatal(view,e)}
}
