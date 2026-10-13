package app

import (
 "context"
 "encoding/json"
 "os"
 "path/filepath"
 "strings"
 "sync"
 "testing"

 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/trainingcheckpoint"
 "model-platform/training/grpcclient"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
)

type checkpointOperations struct{*outputOperation}
func(o *checkpointOperations)EnsureOperation(context.Context,*operationv1.CreateOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){return nil,status.Error(codes.Unavailable,"fixture leaves durable submission pending")}
type checkpointStopped struct{*outputStopped}
func(c *checkpointStopped)CheckpointStopped(ctx context.Context,op *operationv1.Operation)error{return c.OutputStopped(ctx,op)}
func checkpointFixture(t *testing.T)(*outputFixture,trainingcheckpoint.Receipt){
 t.Helper();f:=newOutputFixture(t);ctx:=context.Background()
 spec,_:=NormalizeTrainingSpec(&trainingv1.TrainingSpec{MaxSteps:4,CheckpointSteps:2});raw,_:=json.Marshal(spec)
 if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET training_spec=$2::jsonb WHERE training_run_id=$1`,f.r.RunID,raw);e!=nil{t.Fatal(e)}
 run,e:=f.s.get(ctx,f.r.RunID);if e!=nil{t.Fatal(e)}
 f.mgmt.mv.State="ready";f.mgmt.mv.ArtifactKind="snapshot";f.mgmt.mv.ModelVersionId=f.r.SourceID
 f.s.clients=&grpcclient.Clients{Operation:&checkpointOperations{f.op},Management:f.mgmt}
 f.s.outputControl=&checkpointStopped{f.control}
 r:=trainingcheckpoint.Receipt{Version:1,RunID:f.r.RunID,OperationID:f.r.OperationID,DispatchToken:f.r.DispatchToken,InstanceID:f.r.InstanceID,SourceID:f.r.SourceID,Step:2,MaxSteps:4,Signature:strings.Repeat("a",64)}
 r.Training,_=json.Marshal(run.GetTraining());r.Dataset,_=json.Marshal(run.GetDataset())
 for _,name:=range []string{"adapter_config.json","adapter_model.safetensors","optimizer.pt","scheduler.pt","rng_state.pth","trainer_state.json","training_args.bin"}{r.Files=append(r.Files,trainingcheckpoint.File{Path:name,Size:1,SHA256:strings.Repeat("b",64)});r.Size++}
 saveCheckpointFixture(t,f,r);return f,r
}
func saveCheckpointFixture(t *testing.T,f *outputFixture,r trainingcheckpoint.Receipt){t.Helper();dir:=filepath.Join(f.dir,"checkpoints","checkpoint-2");if e:=os.MkdirAll(dir,0700);e!=nil{t.Fatal(e)};raw,_:=json.Marshal(r);if e:=os.WriteFile(filepath.Join(dir,"receipt.json"),raw,0600);e!=nil{t.Fatal(e)}}
func TestCheckpointResumeConcurrentReplayAndImmutableParent(t *testing.T){
 f,_:=checkpointFixture(t);ctx:=context.Background();parent,e:=f.s.get(ctx,f.r.RunID);if e!=nil{t.Fatal(e)}
 req:=&trainingv1.ResumeTrainingRequest{TrainingRunId:f.r.RunID,CheckpointStep:2,IdempotencyKey:"resume-same-key"}
 out:=make(chan *trainingv1.StartTrainingResponse,6);errs:=make(chan error,6);var wg sync.WaitGroup
 for i:=0;i<6;i++{wg.Add(1);go func(){defer wg.Done();r,e:=f.s.ResumeTraining(ctx,req);out<-r;errs<-e}()};wg.Wait();close(out);close(errs)
 for e:=range errs{if e!=nil{t.Fatal(e)}}
 childID:="";operationID:="";for r:=range out{if childID==""{childID=r.TrainingRunId;operationID=r.OperationId};if r.TrainingRunId!=childID||r.OperationId!=operationID{t.Fatal("duplicate resumed execution",r)}}
 child,e:=f.s.get(ctx,childID);if e!=nil{t.Fatal(e)}
 after,e:=f.s.get(ctx,f.r.RunID);if e!=nil{t.Fatal(e)}
 if !proto.Equal(parent,after)||childID==parent.TrainingRunId||child.OperationId==parent.OperationId||child.GetResume().GetSourceTrainingRunId()!=parent.TrainingRunId||child.GetResume().GetCheckpointStep()!=2||!proto.Equal(child.Training,parent.Training)||!proto.Equal(child.Dataset,parent.Dataset)||child.Status!="created"{t.Fatal("resume mutated execution or settings",child,after)}
 var count int;if e=f.s.pool.QueryRow(ctx,`SELECT count(*) FROM training_runs`).Scan(&count);e!=nil||count!=2{t.Fatal(count,e)}
 // A committed key survives local receipt removal and service reconstruction.
 if e=os.RemoveAll(filepath.Join(f.dir,"checkpoints"));e!=nil{t.Fatal(e)}
 rebuilt:=New(f.s.pool,f.s.clients);again,e:=rebuilt.ResumeTraining(ctx,req)
 if e!=nil||again.TrainingRunId!=childID{t.Fatal("replay depended on local checkpoint",again,e)}
 changed:=proto.Clone(req).(*trainingv1.ResumeTrainingRequest);changed.CheckpointStep=3
 if _,e=rebuilt.ResumeTraining(ctx,changed);status.Code(e)!=codes.AlreadyExists{t.Fatal("same key changed checkpoint",e)}
}
func TestCheckpointResumeRejectsUnstoppedOrIncompatibleParent(t *testing.T){
 for _,which:=range []string{"running","succeeded","wrong_node","wrong_instance","missing_optimizer","max_step","missing_receipt","stop_unconfirmed","changed_dataset","changed_training"}{t.Run(which,func(t *testing.T){
  f,r:=checkpointFixture(t);ctx:=context.Background()
  switch which{
  case "running","succeeded":f.op.op.Status=which;_,_ = f.s.pool.Exec(ctx,`UPDATE training_runs SET status=$2 WHERE training_run_id=$1`,f.r.RunID,which)
  case "wrong_node":r.Node="other"
  case "wrong_instance":r.InstanceID=f.r.OperationID
  case "missing_optimizer":for i,v:=range r.Files{if v.Path=="optimizer.pt"{r.Files=append(r.Files[:i],r.Files[i+1:]...);break}}
  case "max_step":r.MaxSteps=2
  case "changed_dataset":r.Dataset=json.RawMessage(`{"source_type":"s3","format":"chat_messages"}`)
  case "changed_training":r.Training=json.RawMessage(`{"method":"qlora","max_steps":9}`)
  case "stop_unconfirmed":f.control.fail=true
  }
  saveCheckpointFixture(t,f,r)
  if which=="missing_receipt"{_ = os.RemoveAll(filepath.Join(f.dir,"checkpoints"))}
  _,e:=f.s.ResumeTraining(ctx,&trainingv1.ResumeTrainingRequest{TrainingRunId:f.r.RunID,CheckpointStep:2,IdempotencyKey:"denied"})
  if status.Code(e)!=codes.FailedPrecondition{t.Fatal(which,e)}
  var n int;if e=f.s.pool.QueryRow(ctx,`SELECT count(*) FROM training_runs`).Scan(&n);e!=nil||n!=1{t.Fatal("rejected resume inserted child",n,e)}
 })}
}
func TestCancelledCheckpointCanResumeWithoutUncancellingParent(t *testing.T){
 f,_:=checkpointFixture(t);ctx:=context.Background();f.op.op.Status="cancelled"
 if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET status='cancelled',cancel_requested_at=now(),cancelled_at=now() WHERE training_run_id=$1`,f.r.RunID);e!=nil{t.Fatal(e)}
 out,e:=f.s.ListTrainingCheckpoints(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil||len(out.GetCheckpoints())!=1||!out.Checkpoints[0].Resumable{t.Fatal(out,e)}
 child,e:=f.s.ResumeTraining(ctx,&trainingv1.ResumeTrainingRequest{TrainingRunId:f.r.RunID,CheckpointStep:2,IdempotencyKey:"new-run"});if e!=nil||child.TrainingRunId==f.r.RunID{t.Fatal(child,e)}
 parent,e:=f.s.get(ctx,f.r.RunID);if e!=nil||parent.Status!="cancelled"||parent.CancelledAt==nil{t.Fatal(parent,e)}
}
func TestCheckpointResumeRollbackLeavesNoChild(t *testing.T){
 f,_:=checkpointFixture(t);ctx:=context.Background()
 _,e:=f.s.pool.Exec(ctx,`CREATE FUNCTION deny_checkpoint_submission() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'injected submission failure'; END $$; CREATE TRIGGER deny_checkpoint BEFORE INSERT ON operation_submissions FOR EACH ROW EXECUTE FUNCTION deny_checkpoint_submission()`);if e!=nil{t.Fatal(e)}
 req:=&trainingv1.ResumeTrainingRequest{TrainingRunId:f.r.RunID,CheckpointStep:2,IdempotencyKey:"retry-after-rollback"}
 if _,e=f.s.ResumeTraining(ctx,req);e==nil{t.Fatal("submission failure was ignored")}
 var n int;if e=f.s.pool.QueryRow(ctx,`SELECT count(*) FROM training_runs`).Scan(&n);e!=nil||n!=1{t.Fatal("partial child committed",n,e)}
 if _,e=f.s.pool.Exec(ctx,`DROP TRIGGER deny_checkpoint ON operation_submissions`);e!=nil{t.Fatal(e)}
 if _,e=f.s.ResumeTraining(ctx,req);e!=nil{t.Fatal("rolled-back key could not retry",e)}
}
