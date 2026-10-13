package trainingoutput

import (
 "context"
 "errors"
 "testing"
 "github.com/google/uuid"
 "google.golang.org/grpc"
 managementv1 "model-platform/gen/management/v1"
 "model-platform/internal/artifacts"
)
type publishManagement struct{managementv1.ManagementServiceClient;version *managementv1.ModelVersion;completes int;loseReply bool}
func(m *publishManagement)RegisterTrainingOutput(_ context.Context,r *managementv1.RegisterTrainingOutputRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error){if m.version.State=="failed"{m.version.State="importing"};return m.version,nil}
func(m *publishManagement)FailTrainingOutput(context.Context,*managementv1.FailTrainingOutputRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){if m.version.State!="ready"{m.version.State="failed"};return m.version,nil}
func(m *publishManagement)CompleteTrainingOutput(_ context.Context,r *managementv1.CompleteTrainingOutputRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error){m.completes++;m.version.State="ready";m.version.ArtifactUri=r.ArtifactUri;m.version.ArtifactSizeBytes=r.ArtifactSizeBytes;m.version.ArtifactFileCount=r.ArtifactFileCount;if m.loseReply{m.loseReply=false;return nil,errors.New("completion reply lost")};return m.version,nil}
type publishStore struct{manifest artifacts.Manifest;failUpload,failVerify bool;uploads,verifies int}
func(s *publishStore)URI(p string)string{return "s3://test/"+p}
func(s *publishStore)UploadManifest(context.Context,string,string,[]byte)error{s.uploads++;if s.failUpload{return errors.New("storage full")};return nil}
func(s *publishStore)VerifyManifest(context.Context,string,string)(artifacts.Manifest,error){s.verifies++;if s.failVerify{return artifacts.Manifest{},errors.New("corrupt remote object")};return s.manifest,nil}
func TestPublicationRetryUsesSameReservationAfterLostReply(t *testing.T){
 dir,r:=receiptFixture(t);manifest,_:=r.Validate();mgmt:=&publishManagement{version:&managementv1.ModelVersion{ModelId:uuid.NewString(),ModelVersionId:uuid.NewString(),TrainingRunId:r.RunID,SourceModelVersionId:r.SourceID,ArtifactKind:"adapter",ArtifactManifestSha256:r.Digest,State:"importing"},loseReply:true};store:=&publishStore{manifest:manifest}
 call:=func()error{_,e:=Publish(context.Background(),mgmt,store,r.RunID,r.SourceID,dir,manifest,r.Manifest,r.Digest,nil);return e}
 if e:=call();e==nil{t.Fatal("reply loss ignored")};if mgmt.version.State!="ready"{t.Fatal("ready output overwritten by failure")}
 if e:=call();e!=nil{t.Fatal(e)};if mgmt.completes!=1||store.uploads!=1||store.verifies!=2{t.Fatal("reply loss duplicated publication")}
}
func TestPublicationNeverCompletesBeforeVerification(t *testing.T){
 for _,failure:=range []string{"upload","readback","phase","identity"}{t.Run(failure,func(t *testing.T){
  dir,r:=receiptFixture(t);manifest,_:=r.Validate();mgmt:=&publishManagement{version:&managementv1.ModelVersion{ModelId:uuid.NewString(),ModelVersionId:uuid.NewString(),TrainingRunId:r.RunID,SourceModelVersionId:r.SourceID,ArtifactKind:"adapter",ArtifactManifestSha256:r.Digest,State:"importing"}};store:=&publishStore{manifest:manifest};var phase func(string)error
  switch failure{case "upload":store.failUpload=true;case "readback":store.failVerify=true;case "phase":phase=func(string)error{return errors.New("execution cancelled")};case "identity":mgmt.version.TrainingRunId=uuid.NewString()}
  if _,e:=Publish(context.Background(),mgmt,store,r.RunID,r.SourceID,dir,manifest,r.Manifest,r.Digest,phase);e==nil{t.Fatal("failure accepted")};if mgmt.completes!=0{t.Fatal("unverified model completed")}
 })}
}
