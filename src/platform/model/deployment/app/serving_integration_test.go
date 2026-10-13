package app

import (
    "context"
    "errors"
    "strings"
    "sync"
    "testing"
    "time"
    "encoding/json"

    "model-platform/deployment/grpcclient"
    serving "model-platform/deployment/runtime"
    commonv1 "model-platform/gen/common/v1"
    deploymentv1 "model-platform/gen/deployment/v1"
    managementv1 "model-platform/gen/management/v1"
    operationv1 "model-platform/gen/operation/v1"
    "model-platform/internal/testsupport"
    "github.com/google/uuid"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/types/known/timestamppb"
    "google.golang.org/protobuf/proto"
)

type servingManagement struct { managementv1.ManagementServiceClient;version *managementv1.ModelVersion }
func (m *servingManagement) GetModelVersion(context.Context,*managementv1.GetModelVersionRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){return m.version,nil}
func (m *servingManagement) GetModel(context.Context,*managementv1.GetModelRequest,...grpc.CallOption)(*managementv1.Model,error){return &managementv1.Model{ModelType:"llm"},nil}
type servingOperations struct { operationv1.OperationServiceClient;op *operationv1.Operation }
func (o *servingOperations) EnsureOperation(_ context.Context,r *operationv1.CreateOperationRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){
    o.op=&operationv1.Operation{OperationId:r.GetOperationId(),ResourceId:r.GetResourceId(),ResourceType:r.GetResourceType(),ExecutionType:r.GetExecutionType(),Resources:r.GetResources(),Status:"pending",DispatchProtocol:1};return o.op,nil
}
func (o *servingOperations) GetOperation(context.Context,*operationv1.GetOperationRequest,...grpc.CallOption)(*operationv1.Operation,error){return o.op,nil}

type runtimeDriver struct {
    mu sync.Mutex
    evidence *serving.Evidence
    healthy bool
    createGap,startGap bool
    inspectError bool
    creates,starts,stops,images int
}
func (d *runtimeDriver) Image(context.Context)(string,error){d.mu.Lock();defer d.mu.Unlock();d.images++;return "sha256:"+strings.Repeat("a",64),nil}
func (d *runtimeDriver) Inspect(context.Context,serving.Plan)(*serving.Evidence,error){d.mu.Lock();defer d.mu.Unlock();if d.inspectError{return nil,errors.New("foreign or unobservable runtime")};if d.evidence==nil{return nil,nil};copy:=*d.evidence;return &copy,nil}
func (d *runtimeDriver) Create(_ context.Context,p serving.Plan)error{
    d.mu.Lock();defer d.mu.Unlock();d.creates++
    d.evidence=&serving.Evidence{ID:strings.Repeat("b",64),Name:"/"+p.Name(),Image:p.ImageID};d.evidence.State.Status="created"
    json.Unmarshal([]byte(`{"NetworkSettings":{"Ports":{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"18000"}]}}}`),d.evidence)
    if d.createGap {d.createGap=false;return errors.New("create response lost")};return nil
}
func (d *runtimeDriver) Start(context.Context,string)error{d.mu.Lock();defer d.mu.Unlock();d.starts++;d.evidence.State.Status="running";d.evidence.State.Running=true;if d.startGap{d.startGap=false;return errors.New("start response lost")};return nil}
func (d *runtimeDriver) Stop(context.Context,string)error{d.mu.Lock();defer d.mu.Unlock();d.stops++;d.evidence.State.Status="exited";d.evidence.State.Running=false;return nil}
func (d *runtimeDriver) Healthy(context.Context,serving.Plan)(bool,error){d.mu.Lock();defer d.mu.Unlock();return d.healthy,nil}
func (d *runtimeDriver) setHealthy(v bool){d.mu.Lock();defer d.mu.Unlock();d.healthy=v}

type servingFixture struct {s *Service;driver *runtimeDriver;ops *servingOperations;request *deploymentv1.StartServingRequest;sourceID string}
func newServingFixture(t *testing.T) *servingFixture {
    t.Helper();ctx:=context.Background();pool:=testsupport.Postgres(t);if err:=InitSchema(ctx,pool);err!=nil{t.Fatal(err)}
    modelID,sourceID:=uuid.NewString(),uuid.NewString()
    mgmt:=&servingManagement{version:&managementv1.ModelVersion{ModelId:modelID,ModelVersionId:sourceID,State:"ready",SourceType:"huggingface",ArtifactKind:"snapshot",ArtifactUri:"s3://model-artifacts/fixture/",ArtifactSha256:strings.Repeat("a",64),ArtifactSizeBytes:10}}
    ops:=&servingOperations{};s:=New(pool,&grpcclient.Clients{Management:mgmt,Operation:ops});driver:=&runtimeDriver{}
    if err:=s.ConfigureServing(serving.Config{Image:"vllm/vllm-openai:v0.15.0",Network:"ops-infra",Volume:"test-cache",CacheDir:t.TempDir()},driver,serving.FetchFunc(func(context.Context,serving.Plan)error{return nil}));err!=nil{t.Fatal(err)}
    created,err:=s.CreateServingDeployment(ctx,&deploymentv1.DeployModelRequest{ModelVersionId:sourceID,IdempotencyKey:uuid.NewString(),Serving:&deploymentv1.ServingSpec{Engine:"vllm"},Resources:&commonv1.ExecutionResources{GpuIds:[]int32{0}}});if err!=nil{t.Fatal(err)}
    request:=&deploymentv1.StartServingRequest{DeploymentId:created.GetDeploymentId(),OperationId:created.GetOperationId(),DispatchToken:uuid.NewString(),ExecutionInstanceId:uuid.NewString()}
    return &servingFixture{s:s,driver:driver,ops:ops,request:request,sourceID:sourceID}
}
func (f *servingFixture) grant(t *testing.T){t.Helper();f.ops.op.Status="running";f.ops.op.DispatchToken=f.request.DispatchToken;f.ops.op.ExecutionInstanceId=f.request.ExecutionInstanceId;f.ops.op.ExecutionStartedAt=timestamppb.Now();if _,err:=f.s.StartServing(context.Background(),f.request);err!=nil{t.Fatal(err)}}
func (f *servingFixture) cycle(t *testing.T){t.Helper();if err:=f.s.RunServingOnce(context.Background(),f.request.DeploymentId);err!=nil{t.Fatal(err)}}
func (f *servingFixture) view(t *testing.T)*deploymentv1.Deployment{t.Helper();r,err:=f.s.GetDeployment(context.Background(),&deploymentv1.GetDeploymentRequest{DeploymentId:f.request.DeploymentId});if err!=nil{t.Fatal(err)};return r}
func (f *servingFixture) stop(t *testing.T)*deploymentv1.Deployment{t.Helper();r,err:=f.s.StopDeployment(context.Background(),&deploymentv1.StopDeploymentRequest{DeploymentId:f.request.DeploymentId});if err!=nil{t.Fatal(err)};return r}

func TestServingGrantReadinessAndStop(t *testing.T){
    f:=newServingFixture(t);f.cycle(t)
    if f.driver.images!=0{t.Fatal("runtime touched before BeginExecution")}
    if _,err:=f.s.StartServing(context.Background(),f.request);status.Code(err)!=codes.FailedPrecondition{t.Fatal("pending operation authorized serving",err)}
    if f.ops.op.Resources.GetGpuCount()!=0 || len(f.ops.op.Resources.GetGpuIds())!=0{t.Fatal("one-shot coordinator received runtime GPUs")}
    f.grant(t);f.cycle(t);f.cycle(t)
    if r:=f.view(t);r.GetApplyStatus()!="applying" || r.GetEndpoint()!=""{t.Fatal("docker start was treated as readiness",r)}
    f.driver.setHealthy(true);f.cycle(t)
    if r:=f.view(t);r.GetStatus()!="ready" || r.GetRuntimeState()!="ready" || r.GetEndpoint()!="http://127.0.0.1:18000/v1"{t.Fatal("readiness was not committed",r)}
    if _,err:=f.s.CompleteDeployment(context.Background(),&deploymentv1.CompleteDeploymentRequest{DeploymentId:f.request.DeploymentId});status.Code(err)!=codes.FailedPrecondition{t.Fatal("legacy completion accepted for serving",err)}
    if r:=f.stop(t);r.GetStatus()!="stopped" || r.GetApplyStatus()!="ready" || r.GetEndpoint()!=""{t.Fatal("stop changed provisioning history",r)}
    if _,err:=f.s.StartServing(context.Background(),f.request);err!=nil{t.Fatal("same grant replay failed",err)}
    f.cycle(t);f.stop(t)
    if f.driver.creates!=1 || f.driver.starts!=1 || f.driver.stops!=1{t.Fatal("runtime restarted or duplicated")}
    wrong:=proto.Clone(f.request).(*deploymentv1.StartServingRequest);wrong.ExecutionInstanceId=uuid.NewString()
    if _,err:=f.s.StartServing(context.Background(),wrong);status.Code(err)!=codes.FailedPrecondition{t.Fatal("stale process accepted",err)}
}
func TestServingDockerAcknowledgementGaps(t *testing.T){
    for _,gap:=range []string{"create","start"}{t.Run(gap,func(t *testing.T){
        f:=newServingFixture(t);f.driver.createGap=gap=="create";f.driver.startGap=gap=="start";f.driver.setHealthy(true);f.grant(t);f.cycle(t)
        // A new controller instance recovers using persisted identity and Docker evidence.
        if err:=f.s.ConfigureServing(f.s.runtime.config,f.driver,f.s.runtime.fetcher);err!=nil{t.Fatal(err)}
        f.cycle(t);f.cycle(t)
        if r:=f.view(t);r.GetApplyStatus()!="ready"{t.Fatal(r)}
        if f.driver.creates!=1 || f.driver.starts!=1 || f.driver.images!=1{t.Fatal("ambiguous reply caused duplicate runtime or image repinning")}
    })}
}
func TestServingExitDoesNotRestart(t *testing.T){
    f:=newServingFixture(t);f.driver.setHealthy(true);f.grant(t);f.cycle(t);f.cycle(t)
    f.driver.mu.Lock();f.driver.evidence.State.Running=false;f.driver.evidence.State.Status="exited";f.driver.mu.Unlock()
    f.cycle(t)
    if r:=f.view(t);r.GetStatus()!="unhealthy" || r.GetApplyStatus()!="ready" || r.GetEndpoint()!=""{t.Fatal(r)}
    f.cycle(t);if f.driver.starts!=1{t.Fatal("exited runtime restarted")}
    f.driver.mu.Lock();f.driver.evidence=nil;f.driver.mu.Unlock();f.cycle(t)
    if f.driver.creates!=1 || f.view(t).GetApplyStatus()!="ready"{t.Fatal("missing runtime recreated or apply history changed")}
}
func TestServingForeignRuntimeIsNotStopped(t *testing.T){
    f:=newServingFixture(t);f.grant(t);f.driver.inspectError=true;f.cycle(t)
    r:=f.stop(t)
    if r.GetStatus()!="stopping" || f.driver.stops!=0 || f.driver.creates!=0{t.Fatal("unconfirmed runtime modified",r)}
}
func TestServingStopBeforeGrant(t *testing.T){
    f:=newServingFixture(t)
    if r:=f.stop(t);r.GetStatus()!="stopped" || r.GetApplyStatus()!="failed"{t.Fatal(r)}
    if _,err:=f.s.StartServing(context.Background(),f.request);status.Code(err)!=codes.FailedPrecondition{t.Fatal("stopped deployment accepted new work",err)}
    f.cycle(t);if f.driver.creates!=0 || f.driver.images!=0{t.Fatal("stopped request launched")}
}
func TestServingStopCancelsPreparation(t *testing.T){
    f:=newServingFixture(t);entered:=make(chan struct{});done:=make(chan error,1)
    f.s.runtime.fetcher=serving.FetchFunc(func(ctx context.Context,_ serving.Plan)error{close(entered);<-ctx.Done();return ctx.Err()})
    f.grant(t);go func(){done<-f.s.RunServingOnce(context.Background(),f.request.DeploymentId)}()
    select{case <-entered:case <-time.After(5*time.Second):t.Fatal("preparation not reached")}
    f.stop(t)
    select{case err:=<-done:if err!=nil{t.Fatal(err)};case <-time.After(5*time.Second):t.Fatal("stop did not cancel preparation")}
    if r:=f.view(t);r.GetStatus()!="stopped" || f.driver.creates!=0{t.Fatal("stop raced into runtime launch",r)}
}
func TestServingPreparationFailureAndReadinessDeadline(t *testing.T){
    t.Run("integrity",func(t *testing.T){f:=newServingFixture(t);f.s.runtime.fetcher=serving.FetchFunc(func(context.Context,serving.Plan)error{return errors.New("hash mismatch")});f.grant(t);f.cycle(t);if f.view(t).GetRuntimeErrorCode()!="SNAPSHOT_PREPARATION_FAILED" || f.driver.creates!=0{t.Fatal("unverified snapshot launched")}})
    t.Run("deadline",func(t *testing.T){f:=newServingFixture(t);f.grant(t);f.cycle(t)
        if _,err:=f.s.pool.Exec(context.Background(),`UPDATE deployments SET runtime_launch_at=clock_timestamp()-interval '16 minutes' WHERE deployment_id=$1`,f.request.DeploymentId);err!=nil{t.Fatal(err)}
        f.cycle(t);f.cycle(t);if r:=f.view(t);r.GetApplyStatus()!="failed" || r.GetRuntimeErrorCode()!="READINESS_TIMEOUT"{t.Fatal(r)}
        if f.driver.starts!=1{t.Fatal("timeout restarted runtime")};f.stop(t)
    })
}
func TestServingReadyCommitCanRecover(t *testing.T){
    f:=newServingFixture(t);f.grant(t);f.driver.setHealthy(true);f.cycle(t)
    if _,err:=f.s.pool.Exec(context.Background(),`ALTER TABLE deployments ADD CONSTRAINT reject_serving_ready CHECK(apply_status<>'ready')`);err!=nil{t.Fatal(err)}
    if err:=f.s.RunServingOnce(context.Background(),f.request.DeploymentId);err==nil{t.Fatal("failed ready commit hidden")}
    if _,err:=f.s.pool.Exec(context.Background(),`ALTER TABLE deployments DROP CONSTRAINT reject_serving_ready`);err!=nil{t.Fatal(err)}
    f.cycle(t);if f.view(t).GetApplyStatus()!="ready" || f.driver.starts!=1{t.Fatal("ready commit recovery restarted runtime")}
}
