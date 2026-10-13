package creation_test

import (
 "context"
 "errors"
 "encoding/json"
 "strings"
 "testing"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "github.com/google/uuid"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
type controlFake struct{stops int;failure bool}
func(c *controlFake)Cancel(_ context.Context,op *operationv1.Operation)error{c.stops++;if op.GetStatus()!="cancelling"{return errors.New("not fenced")};if c.failure{return errors.New("stop reply lost")};return nil}
func(c *controlFake)Logs(_ context.Context,op *operationv1.Operation,lines,max int)(*trainingv1.TrainingLogs,error){return &trainingv1.TrainingLogs{OperationId:op.GetOperationId(),TrainingRunId:op.GetResourceId(),Source:"docker",Availability:"available",Stdout:"fixture log\n",TailLines:int32(lines),MaxBytes:int32(max)},nil}
func newCancellationRun(t *testing.T,f *fixture)*trainingv1.StartTrainingResponse{
 t.Helper();r,err:=f.clients.Training.StartTraining(context.Background(),&trainingv1.StartTrainingRequest{SourceModelVersionId:f.versionID,IdempotencyKey:uuid.NewString()});if err!=nil{t.Fatal(err)};return r
}
func TestCancellationRESTPersistenceStopRetryAndLateReports(t *testing.T){
 f:=newFixture(t);control:=&controlFake{failure:true};f.trainer.ConfigureJobs(control);r:=newCancellationRun(t,f);ctx:=context.Background()
 path:="/api/training-runs/"+r.GetTrainingRunId()
 code,first:=f.request(t,"POST",path+"/cancel","");if code!=202||first["status"]!="cancelling"||first["cancel_requested_at"]==nil{t.Fatal(code,first)}
 code,again:=f.request(t,"POST",path+"/cancel","");if code!=202||again["cancel_requested_at"]!=first["cancel_requested_at"]{t.Fatal("request replay changed",again)}
 if err:=f.trainer.ProcessCancellation(ctx,r.GetTrainingRunId());err!=nil{t.Fatal(err)}
 code,pending:=f.request(t,"GET",path,"");if code!=200||pending["status"]!="cancelling"||pending["cancellation_error_code"]!="CANCEL_STOP_UNCONFIRMED"{t.Fatal(pending)}
 claim,err:=f.clients.Operation.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OperationId:r.OperationId,OwnerId:"late"});if err!=nil||claim.GetClaimed(){t.Fatal("cancelled job dispatched",err)}
 _,err=f.clients.Training.FailTraining(ctx,&trainingv1.FailTrainingRequest{TrainingRunId:r.TrainingRunId});if status.Code(err)!=codes.FailedPrecondition{t.Fatal("late failure changed cancellation",err)}
 control.failure=false;execSQL(t,f.training,`UPDATE training_runs SET cancellation_checked_at=NULL WHERE training_run_id=$1`,r.TrainingRunId)
 if err=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 code,done:=f.request(t,"POST",path+"/cancel","");if code!=200||done["status"]!="cancelled"||done["cancelled_at"]==nil||done["output_model_version_id"]!=""{t.Fatal(done)}
 op,err:=f.clients.Operation.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:r.OperationId});if err!=nil||op.Status!="cancelled"{t.Fatal(op,err)}
 if count(t,f.ops,`SELECT count(*) FROM operations`)!=1||count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("cancellation created a second execution")}
 code,logs:=f.request(t,"GET",path+"/logs?tail_lines=10&max_bytes=1024","");if code!=200||logs["stdout"]!="fixture log\n"||logs["operation_id"]!=r.OperationId{t.Fatal(logs)}
 items,_:=listItems(t,f,"/api/training-runs?status=cancelled");if len(items)!=1||items[0].(map[string]any)["cancelled_at"]==nil{t.Fatal(items)}
}
func TestCancellationSurvivesOperationCommitAndDomainRollback(t *testing.T){
 f:=newFixture(t);control:=&controlFake{};f.trainer.ConfigureJobs(control);r:=newCancellationRun(t,f);ctx:=context.Background()
 if _,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});err!=nil{t.Fatal(err)}
 execSQL(t,f.training,`ALTER TABLE training_runs ADD CONSTRAINT deny_cancel_commit CHECK(status!='cancelled') NOT VALID`)
 if err:=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err==nil{t.Fatal("injected rollback was ignored")}
 op,err:=f.clients.Operation.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:r.OperationId});if err!=nil||op.Status!="cancelled"{t.Fatal(op,err)}
 execSQL(t,f.training,`ALTER TABLE training_runs DROP CONSTRAINT deny_cancel_commit`)
 if err=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 run,err:=f.clients.Training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if err!=nil||run.Status!="cancelled"||control.stops!=1{t.Fatal("restart did not finish prior cancellation",run,control.stops,err)}
}
func TestCancellationBeforeSubmissionAndCompletedReplay(t *testing.T){
 f:=newFixture(t);control:=&controlFake{};f.trainer.ConfigureJobs(control);ctx:=context.Background()
 f.failEnsure.Store(int32(codes.Unavailable));r:=newCancellationRun(t,f)
 if _,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});err!=nil{t.Fatal(err)}
 if err:=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 if control.stops!=0{t.Fatal("stopped without operation identity")}
 f.failEnsure.Store(0);execSQL(t,f.training,`UPDATE training_runs SET cancellation_checked_at=NULL WHERE training_run_id=$1`,r.TrainingRunId)
 if err:=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 f.due(t,f.training);if _,err:=f.worker(f.training).Deliver(ctx,r.OperationId);err!=nil{t.Fatal(err)}
 op,err:=f.clients.Operation.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:r.OperationId});if err!=nil||op.Status!="cancelled"{t.Fatal("late submission revived cancellation",op,err)}
 other:=newCancellationRun(t,f);output:=uuid.NewString()
 execSQL(t,f.training,`UPDATE training_runs SET status='succeeded',output_model_version_id=$2,result_json='{"saved":true}' WHERE training_run_id=$1`,other.TrainingRunId,output)
 done,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:other.TrainingRunId});if err!=nil||done.Status!="succeeded"||done.OutputModelVersionId!=output||done.CancelRequestedAt!=nil{t.Fatal("completed training was changed",done,err)}
}

func TestTrainingCompletionAndCancellationRace(t *testing.T){
 f:=newFixture(t);f.trainer.ConfigureJobs(&controlFake{});ctx:=context.Background()
 for i:=0;i<8;i++{
  r:=newCancellationRun(t,f);output:=uuid.NewString();digest:=strings.Repeat("a",64);uri:="s3://model-artifacts/"+output+"/"
  execSQL(t,f.management,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind,training_run_id,source_model_version_id,artifact_uri,artifact_sha256,artifact_size_bytes,artifact_manifest_sha256,artifact_file_count) VALUES($1::uuid,$2,$1::text,'ready','training','adapter',$3,$4,$5,$6,10,$6,2)`,output,f.modelID,r.TrainingRunId,f.versionID,uri,digest)
  raw,_:=json.Marshal(map[string]any{"train_exit_code":0,"output_file_count":2,"output_model_version_id":output,"output_artifact_uri":uri,"output_manifest_sha256":digest})
  start:=make(chan struct{});cancelDone:=make(chan error,1);completeDone:=make(chan error,1)
  go func(){<-start;_,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});cancelDone<-err}()
  go func(){<-start;_,err:=f.clients.Training.CompleteTraining(ctx,&trainingv1.CompleteTrainingRequest{TrainingRunId:r.TrainingRunId,OutputModelVersionId:output,ResultJson:string(raw)});completeDone<-err}()
  close(start);if err:=<-cancelDone;err!=nil{t.Fatal(err)};completionErr:=<-completeDone
  got,err:=f.clients.Training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if err!=nil{t.Fatal(err)}
  switch got.Status{
  case "succeeded":if completionErr!=nil||got.OutputModelVersionId!=output||got.CancelRequestedAt!=nil{t.Fatal("completed outcome damaged",got,completionErr)}
  case "cancelling":if status.Code(completionErr)!=codes.FailedPrecondition||got.OutputModelVersionId!=""||got.ResultJson!=""{t.Fatal("cancellation did not block completion",got,completionErr)}
  default:t.Fatal("unexpected race outcome",got)
  }
 }
}

func TestCancellationTrackingRetryDoesNotRepeatStop(t *testing.T){
 f:=newFixture(t);control:=&controlFake{};f.trainer.ConfigureJobs(control);r:=newCancellationRun(t,f);ctx:=context.Background()
 execSQL(t,f.training,`UPDATE training_runs SET mlflow_run_id='tracking' WHERE training_run_id=$1`,r.TrainingRunId)
 if _,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});err!=nil{t.Fatal(err)}
 f.trainer.ConfigureTracking(func(context.Context,string)error{return errors.New("tracking offline")})
 if err:=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 first,err:=f.clients.Training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if err!=nil||first.Status!="cancelled"||first.CancellationErrorCode!="CANCEL_TRACKING_SYNC_PENDING"{t.Fatal(first,err)}
 f.trainer.ConfigureTracking(func(context.Context,string)error{return nil});execSQL(t,f.training,`UPDATE training_runs SET cancellation_checked_at=NULL WHERE training_run_id=$1`,r.TrainingRunId)
 if err=f.trainer.ProcessCancellation(ctx,r.TrainingRunId);err!=nil{t.Fatal(err)}
 last,err:=f.clients.Training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if err!=nil||last.CancellationErrorCode!=""||!last.CancelledAt.AsTime().Equal(first.CancelledAt.AsTime())||control.stops!=1{t.Fatal(last,control.stops,err)}
}

func TestRemoteCancellationAndLogsAreRejectedBeforeControl(t *testing.T){
 f:=newFixture(t);control:=&controlFake{};f.trainer.ConfigureJobs(control);r:=newCancellationRun(t,f);ctx:=context.Background()
 execSQL(t,f.training,`UPDATE training_runs SET resources='{"node_name":"unsupported-remote","gpu_count":1}' WHERE training_run_id=$1`,r.TrainingRunId)
 _,err:=f.clients.Training.CancelTraining(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if status.Code(err)!=codes.FailedPrecondition{t.Fatal(err)}
 _,err=f.clients.Training.GetTrainingLogs(ctx,&trainingv1.GetTrainingLogsRequest{TrainingRunId:r.TrainingRunId});if status.Code(err)!=codes.FailedPrecondition||control.stops!=0{t.Fatal(err)}
 run,err:=f.clients.Training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:r.TrainingRunId});if err!=nil||run.Status!="created"||run.CancelRequestedAt!=nil{t.Fatal(run,err)}
}
