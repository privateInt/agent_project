package app

import (
 "context"
 "encoding/json"
 "errors"
 "os"
 "path/filepath"
 "sync"
 "testing"
 "github.com/google/uuid"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
 "google.golang.org/protobuf/types/known/timestamppb"
 commonv1 "model-platform/gen/common/v1"
 managementv1 "model-platform/gen/management/v1"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/artifacts"
 "model-platform/internal/testsupport"
 "model-platform/internal/trainingoutput"
 "model-platform/training/grpcclient"
)

type outputOperation struct{operationv1.OperationServiceClient;op *operationv1.Operation}
func(o *outputOperation)GetOperation(context.Context,*operationv1.GetOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){return proto.Clone(o.op).(*operationv1.Operation),nil}
type outputStopped struct{fail bool}
func(c *outputStopped)OutputStopped(context.Context,*operationv1.Operation)error{if c.fail{return errors.New("producer still running")};return nil}
type outputMemoryStore struct{manifest artifacts.Manifest;fail bool;uploads int;wait chan struct{};entered chan struct{}}
func(s *outputMemoryStore)URI(prefix string)string{return "s3://test/"+prefix}
func(s *outputMemoryStore)UploadManifest(ctx context.Context,root,prefix string,raw []byte)error{
 s.uploads++;if s.entered!=nil{close(s.entered);s.entered=nil};if s.wait!=nil{select{case <-s.wait:case <-ctx.Done():return ctx.Err()}}
 if s.fail{return errors.New("storage full")};m,b,_,e:=artifacts.Build(root);if e!=nil{return e};if string(raw)!=string(b){return errors.New("unexpected bytes")};s.manifest=m;return nil
}
func(s *outputMemoryStore)VerifyManifest(context.Context,string,string)(artifacts.Manifest,error){if s.fail{return artifacts.Manifest{},errors.New("readback unavailable")};return s.manifest,nil}
type outputManagement struct{managementv1.ManagementServiceClient;mv *managementv1.ModelVersion;completes int;loseReply bool}
func(m *outputManagement)GetModelVersion(context.Context,*managementv1.GetModelVersionRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){return proto.Clone(m.mv).(*managementv1.ModelVersion),nil}
func(m *outputManagement)RegisterTrainingOutput(context.Context,*managementv1.RegisterTrainingOutputRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){if m.mv.State=="failed"{m.mv.State="importing"};return proto.Clone(m.mv).(*managementv1.ModelVersion),nil}
func(m *outputManagement)FailTrainingOutput(context.Context,*managementv1.FailTrainingOutputRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){if m.mv.State!="ready"{m.mv.State="failed"};return proto.Clone(m.mv).(*managementv1.ModelVersion),nil}
func(m *outputManagement)CompleteTrainingOutput(_ context.Context,r *managementv1.CompleteTrainingOutputRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error){m.completes++;m.mv.State="ready";m.mv.ArtifactUri=r.ArtifactUri;m.mv.ArtifactSizeBytes=r.ArtifactSizeBytes;m.mv.ArtifactFileCount=r.ArtifactFileCount;if m.loseReply{m.loseReply=false;return nil,errors.New("lost reply")};return proto.Clone(m.mv).(*managementv1.ModelVersion),nil}
type outputFixture struct{s *Service;r trainingoutput.Receipt;dir string;op *outputOperation;control *outputStopped;store *outputMemoryStore;mgmt *outputManagement}
func newOutputFixture(t *testing.T)*outputFixture{
 t.Helper();ctx:=context.Background();pool:=testsupport.Postgres(t);if e:=InitSchema(ctx,pool);e!=nil{t.Fatal(e)}
 r:=trainingoutput.Receipt{Version:1,RunID:uuid.NewString(),OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),InstanceID:uuid.NewString(),SourceID:uuid.NewString()}
 root:=t.TempDir();dir,e:=trainingoutput.Directory(root,r.RunID,r.DispatchToken);if e!=nil{t.Fatal(e)};if e=os.MkdirAll(filepath.Join(dir,"output"),0700);e!=nil{t.Fatal(e)}
 for name,value:=range map[string]string{"adapter_config.json":"{}","adapter_model.safetensors":"weights"}{if e=os.WriteFile(filepath.Join(dir,"output",name),[]byte(value),0600);e!=nil{t.Fatal(e)}}
 m,b,d,e:=artifacts.Build(filepath.Join(dir,"output"));if e!=nil{t.Fatal(e)};r.Manifest=b;r.Digest=d
 r.Result,_=json.Marshal(map[string]any{"train_exit_code":0,"source_model_version_id":r.SourceID,"output_file_count":m.FileCount,"output_bytes":m.TotalBytes,"dataset_source_type":"synthetic","dataset_source_uri":"","dataset_format":"chat_messages"})
 if e=trainingoutput.Seal(dir,r);e!=nil{t.Fatal(e)}
 if _,e=pool.Exec(ctx,`INSERT INTO training_runs(training_run_id,source_model_version_id,status,resources,result_json) VALUES($1,$2,'failed','{"gpu_count":1}','{"original":"failure"}')`,r.RunID,r.SourceID);e!=nil{t.Fatal(e)}
 if _,e=pool.Exec(ctx,`INSERT INTO operation_submissions(operation_id,resource_type,resource_id,request,status) VALUES($1,'training_run',$2,$3,'delivered')`,r.OperationID,r.RunID,[]byte{1});e!=nil{t.Fatal(e)}
 op:=&outputOperation{op:&operationv1.Operation{OperationId:r.OperationID,ResourceId:r.RunID,ResourceType:"training_run",ExecutionType:"training.run",DispatchProtocol:1,Status:"failed",Phase:"failed",DispatchToken:r.DispatchToken,ExecutionInstanceId:r.InstanceID,ExecutionStartedAt:timestamppb.Now(),Resources:&commonv1.ExecutionResources{GpuCount:1}}}
 mgmt:=&outputManagement{mv:&managementv1.ModelVersion{ModelId:uuid.NewString(),ModelVersionId:uuid.NewString(),TrainingRunId:r.RunID,SourceModelVersionId:r.SourceID,ArtifactKind:"adapter",ArtifactManifestSha256:r.Digest,State:"importing"}}
 s:=New(pool,&grpcclient.Clients{Operation:op,Management:mgmt});store:=&outputMemoryStore{manifest:m};control:=&outputStopped{}
 s.ConfigureOutputs(root,"",func(context.Context)(trainingoutput.Store,error){return store,nil},control)
 return &outputFixture{s:s,r:r,dir:dir,op:op,control:control,store:store,mgmt:mgmt}
}
func(f *outputFixture)retry(t *testing.T,key string)*trainingv1.TrainingOutput{t.Helper();out,e:=f.s.RetryTrainingOutput(context.Background(),&trainingv1.RetryTrainingOutputRequest{TrainingRunId:f.r.RunID,IdempotencyKey:key});if e!=nil{t.Fatal(e)};return out}
func(f *outputFixture)get(t *testing.T)*trainingv1.TrainingOutput{t.Helper();o,e:=f.s.GetTrainingOutput(context.Background(),&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil{t.Fatal(e)};return o}
func(f *outputFixture)process(t *testing.T){t.Helper();if e:=f.s.ProcessOutputRecovery(context.Background(),f.r.RunID);e!=nil{t.Fatal(e)}}

func TestOutputRecoveryPreservesFailureAndRetriesPublicationOnly(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();f.store.fail=true;first:=f.retry(t,"attempt-one");f.process(t)
 if got:=f.get(t);got.RecoveryStatus!="failed"||got.ErrorCode!="OUTPUT_PUBLICATION_FAILED"||got.Availability!="retained"{t.Fatal(got)}
 if got:=f.retry(t,"attempt-one");got.RecoveryId!=first.RecoveryId||got.RecoveryStatus!="failed"{t.Fatal("failed key created a new attempt",got)}
 f.store.fail=false;second:=f.retry(t,"attempt-two");if second.RecoveryId==first.RecoveryId{t.Fatal("new key did not create retry")};f.process(t)
 got:=f.get(t);if got.RecoveryStatus!="succeeded"||got.OutputModelVersionId!=f.mgmt.mv.ModelVersionId{t.Fatal(got)}
 run,e:=f.s.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil||run.Status!="failed"||run.ResultJson!=`{"original":"failure"}`||run.OutputModelVersionId!=""||run.RecoveredModelVersionId!=got.OutputModelVersionId||f.op.op.Status!="failed"{t.Fatal("original execution outcome changed",run,e)}
 // Replay the older key returns its own failed attempt, not the latest success.
 if old:=f.retry(t,"attempt-one");old.RecoveryId!=first.RecoveryId||old.RecoveryStatus!="failed"||old.OutputModelVersionId!=""{t.Fatal(old)}
}
func TestOutputRecoveryFencesMutationAndCancelledOrRunningProducer(t *testing.T){
 for _,which:=range []string{"running","cancelled","stop_unconfirmed","changed_token","wrong_node","wrong_instance","changed_dataset","changed_resources","changed_file","missing_receipt"}{t.Run(which,func(t *testing.T){
  f:=newOutputFixture(t);ctx:=context.Background()
  if which=="changed_file"{f.retry(t,"key");if e:=os.WriteFile(filepath.Join(f.dir,"output","adapter_model.safetensors"),[]byte("tampered"),0600);e!=nil{t.Fatal(e)};f.process(t);if got:=f.get(t);got.ErrorCode!="SAVED_OUTPUT_CHANGED"||f.store.uploads!=0{t.Fatal(got)};return}
  switch which{
  case "running":f.op.op.Status="running"
  case "cancelled":if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET status='cancelled',cancel_requested_at=now() WHERE training_run_id=$1`,f.r.RunID);e!=nil{t.Fatal(e)}
  case "stop_unconfirmed":f.control.fail=true
  case "changed_token":f.op.op.DispatchToken=uuid.NewString()
  case "wrong_node":f.s.outputNode="other-node"
  case "wrong_instance":f.op.op.ExecutionInstanceId=uuid.NewString()
  case "changed_resources":f.op.op.Resources.GpuCount=2
  case "changed_dataset":if _,e:=f.s.pool.Exec(ctx,`UPDATE training_runs SET dataset_spec='{"source_type":"s3","source_uri":"s3://other/input.jsonl","format":"chat_messages"}' WHERE training_run_id=$1`,f.r.RunID);e!=nil{t.Fatal(e)}
  case "missing_receipt":if e:=os.Remove(filepath.Join(f.dir,trainingoutput.ReceiptName));e!=nil{t.Fatal(e)}
  }
  _,e:=f.s.RetryTrainingOutput(ctx,&trainingv1.RetryTrainingOutputRequest{TrainingRunId:f.r.RunID,IdempotencyKey:"key"});if status.Code(e)!=codes.FailedPrecondition||f.store.uploads!=0{t.Fatal(which,e)}
 })}
}
func TestOutputRecoveryRestartAfterRemoteCommitAndDBRollback(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();f.retry(t,"key")
 if _,e:=f.s.pool.Exec(ctx,`ALTER TABLE training_output_recoveries ADD CONSTRAINT reject_success CHECK(status!='succeeded') NOT VALID`);e!=nil{t.Fatal(e)}
 if e:=f.s.ProcessOutputRecovery(ctx,f.r.RunID);e==nil{t.Fatal("injected DB failure ignored")}
 if f.mgmt.mv.State!="ready"||f.get(t).RecoveryStatus!="running"{t.Fatal("partial completion missing")}
 if _,e:=f.s.pool.Exec(ctx,`ALTER TABLE training_output_recoveries DROP CONSTRAINT reject_success`);e!=nil{t.Fatal(e)}
 restarted:=New(f.s.pool,f.s.clients);restarted.ConfigureOutputs(f.s.outputRoot,"",f.s.outputStore,f.control)
 if e:=restarted.ProcessOutputRecovery(ctx,f.r.RunID);e!=nil{t.Fatal(e)}
 if f.get(t).RecoveryStatus!="succeeded"||f.store.uploads!=1||f.mgmt.completes!=1{t.Fatal("restart duplicated publication")}
}
func TestOutputRecoveryConcurrencyAndCleanupGuard(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();f.retry(t,"one")
 f.store.entered=make(chan struct{});entered:=f.store.entered;f.store.wait=make(chan struct{});done:=make(chan error,1)
 go func(){done<-f.s.ProcessOutputRecovery(ctx,f.r.RunID)}();<-entered
 if e:=f.s.ProcessOutputRecovery(ctx,f.r.RunID);e==nil{t.Fatal("parallel publisher entered output lock")}
 if _,e:=f.s.ReleaseTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});status.Code(e)!=codes.FailedPrecondition{t.Fatal("active output deleted",e)}
 close(f.store.wait);if e:=<-done;e!=nil{t.Fatal(e)}
 f.store.fail=true;if _,e:=f.s.ReleaseTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});status.Code(e)!=codes.FailedPrecondition{t.Fatal("cleanup ignored remote verification",e)}
 if e:=trainingoutput.Verify(f.dir,f.r);e!=nil{t.Fatal("failed cleanup lost local data",e)}
 f.store.fail=false;out,e:=f.s.ReleaseTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil||out.Availability!="released"{t.Fatal(out,e)}
 if _,e=os.Stat(filepath.Join(f.dir,"output"));!os.IsNotExist(e){t.Fatal("local output not removed")}
 if f.retry(t,"one").RecoveryStatus!="succeeded"{t.Fatal("cleanup broke idempotent replay")}
 out,e=f.s.ReleaseTrainingOutput(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:f.r.RunID});if e!=nil||out.Availability!="released"{t.Fatal("cleanup replay",out,e)}
}
func TestOutputRecoveryConcurrentSameKeyCreatesOneAttempt(t *testing.T){
 f:=newOutputFixture(t);ctx:=context.Background();var wg sync.WaitGroup;results:=make(chan *trainingv1.TrainingOutput,2);errs:=make(chan error,2)
 for i:=0;i<2;i++{wg.Add(1);go func(){defer wg.Done();o,e:=f.s.RetryTrainingOutput(ctx,&trainingv1.RetryTrainingOutputRequest{TrainingRunId:f.r.RunID,IdempotencyKey:"same"});results<-o;errs<-e}()};wg.Wait()
 for i:=0;i<2;i++{if e:=<-errs;e!=nil{t.Fatal(e)}};a,b:=<-results,<-results;if a.RecoveryId!=b.RecoveryId{t.Fatal("duplicate publication requests")}
}
