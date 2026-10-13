package app

import (
	"context"
	"errors"
	"strings"
	"testing"

	serving "model-platform/deployment/runtime"
	commonv1 "model-platform/gen/common/v1"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type adapterManagement struct {
	managementv1.ManagementServiceClient
	versions map[string]*managementv1.ModelVersion
	unavailable bool
}
func (m *adapterManagement) GetModelVersion(_ context.Context,r *managementv1.GetModelVersionRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error) {
	if m.unavailable {return nil,status.Error(codes.Unavailable,"offline")}
	if v:=m.versions[r.GetModelVersionId()];v!=nil {return v,nil}
	return nil,status.Error(codes.NotFound,"missing")
}
func (m *adapterManagement) GetModel(context.Context,*managementv1.GetModelRequest,...grpc.CallOption)(*managementv1.Model,error) {
	return &managementv1.Model{ModelType:"llm"},nil
}
func adapterRequest(t *testing.T,f *servingFixture)(*deploymentv1.DeployModelRequest,*adapterManagement) {
	t.Helper()
	base:=f.s.clients.Management.(*servingManagement).version
	adapter:=&managementv1.ModelVersion{
		ModelVersionId:uuid.NewString(),ModelId:base.ModelId,State:"ready",SourceType:"training",ArtifactKind:"adapter",
		TrainingRunId:uuid.NewString(),SourceModelVersionId:base.ModelVersionId,BaseModelArtifactUri:base.ArtifactUri,
		ArtifactUri:"s3://model-artifacts/adapter/",ArtifactSha256:strings.Repeat("b",64),ArtifactManifestSha256:strings.Repeat("b",64),ArtifactSizeBytes:30,ArtifactFileCount:2,
	}
	m:=&adapterManagement{versions:map[string]*managementv1.ModelVersion{base.ModelVersionId:base,adapter.ModelVersionId:adapter}}
	f.s.clients.Management=m
	return &deploymentv1.DeployModelRequest{ModelVersionId:adapter.ModelVersionId,IdempotencyKey:uuid.NewString(),Serving:&deploymentv1.ServingSpec{Engine:"vllm"},Resources:&commonv1.ExecutionResources{GpuIds:[]int32{0}}},m
}
func TestAdapterServingLifecycleAndReplay(t *testing.T) {
	f:=newServingFixture(t);req,m:=adapterRequest(t,f);ctx:=context.Background()
	created,err:=f.s.CreateServingDeployment(ctx,req);if err!=nil{t.Fatal(err)}
	f.request.DeploymentId,f.request.OperationId=created.DeploymentId,created.OperationId
	var engine string
	if err:=f.s.pool.QueryRow(ctx,`SELECT engine FROM deployments WHERE deployment_id=$1`,created.DeploymentId).Scan(&engine);err!=nil{t.Fatal(err)}
	if engine!="vllm-lora" {t.Fatal("old snapshot-only controller could launch an unadapted base")}
	var captured serving.Plan
	f.s.runtime.fetcher=serving.FetchFunc(func(_ context.Context,p serving.Plan)error{captured=p;return nil})
	f.grant(t);f.driver.setHealthy(true);f.cycle(t);f.cycle(t)
	r:=f.view(t)
	if r.GetArtifactKind()!="adapter" || r.GetAdapterModelVersionId()!=req.ModelVersionId || r.GetModelVersionId()!=req.ModelVersionId || r.GetBaseModelVersionId()!=f.sourceID || r.GetServing().GetEngine()!="vllm" || r.GetBaseServedModelName()==r.GetServedModelName() {t.Fatal("adapter lineage not returned",r)}
	if captured.Source.ModelVersionID!=f.sourceID || captured.Source.Adapter.ModelVersionID!=req.ModelVersionId || captured.Source.Adapter.ServingFormat!=2 {t.Fatal("wrong captured source",captured)}
	if r:=f.stop(t);r.GetStatus()!="stopped" || r.GetApplyStatus()!="ready" {t.Fatal(r)}
	m.unavailable=true // Replaying an accepted request must not resolve the base again.
	again,err:=f.s.CreateServingDeployment(ctx,req)
	if err!=nil || again.GetDeploymentId()!=created.DeploymentId || again.GetOperationId()!=created.OperationId {t.Fatal("accepted adapter replay changed",again,err)}
	f.cycle(t);if f.driver.creates!=1 || f.driver.starts!=1 {t.Fatal("stopped adapter replay relaunched")}
	req.Serving.MaxModelLen=1024
	if _,err:=f.s.CreateServingDeployment(ctx,req);status.Code(err)!=codes.AlreadyExists {t.Fatal("same key accepted different serving spec",err)}
}
func TestAdapterLineageValidationRollsBack(t *testing.T) {
	for _,failure:=range []string{"missing-base","wrong-parent-uri","wrong-model","not-ready","bad-manifest","not-training"} {
		t.Run(failure,func(t *testing.T){
			f:=newServingFixture(t);req,m:=adapterRequest(t,f);a:=m.versions[req.ModelVersionId];base:=m.versions[f.sourceID]
			switch failure {case "missing-base":a.SourceModelVersionId="";case "wrong-parent-uri":a.BaseModelArtifactUri+="other/";case "wrong-model":base.ModelId=uuid.NewString();case "not-ready":base.State="failed";case "bad-manifest":a.ArtifactSha256=strings.Repeat("c",64);case "not-training":a.SourceType="upload"}
			if _,err:=f.s.CreateServingDeployment(context.Background(),req);status.Code(err)!=codes.FailedPrecondition {t.Fatal(failure,err)}
			var count int
			if err:=f.s.pool.QueryRow(context.Background(),`SELECT count(*) FROM deployments`).Scan(&count);err!=nil{t.Fatal(err)}
			if count!=1 {t.Fatal("rejected adapter created a domain resource")}
		})
	}
}
func TestAdapterPreparationFailureNeverLaunches(t *testing.T) {
	f:=newServingFixture(t);req,_:=adapterRequest(t,f)
	r,err:=f.s.CreateServingDeployment(context.Background(),req);if err!=nil{t.Fatal(err)}
	f.request.DeploymentId,f.request.OperationId=r.DeploymentId,r.OperationId
	f.s.runtime.fetcher=serving.FetchFunc(func(context.Context,serving.Plan)error{return errors.New("saved embedding differs from base")})
	f.grant(t);f.cycle(t)
	if f.driver.creates!=0 || f.view(t).GetRuntimeErrorCode()!="ADAPTER_PREPARATION_FAILED" {t.Fatal("invalid adapter was launched")}
	f.stop(t)
}

func TestLegacyAdapterPlanRemainsManageable(t *testing.T) {
	f:=newServingFixture(t);req,m:=adapterRequest(t,f);ctx:=context.Background()
	r,err:=f.s.CreateServingDeployment(ctx,req);if err!=nil{t.Fatal(err)}
	f.request.DeploymentId,f.request.OperationId=r.DeploymentId,r.OperationId
	// Simulate a request persisted by the previously deployed format-1 code.
	if _,err:=f.s.pool.Exec(ctx,`UPDATE deployments SET serving_source=jsonb_set(serving_source,'{adapter,serving_format}','1'::jsonb) WHERE deployment_id=$1`,r.DeploymentId);err!=nil{t.Fatal(err)}
	var captured serving.Plan
	f.s.runtime.fetcher=serving.FetchFunc(func(_ context.Context,p serving.Plan)error{captured=p;return nil})
	f.grant(t);f.driver.setHealthy(true);f.cycle(t);f.cycle(t)
	if captured.Source.Adapter==nil || captured.Source.Adapter.ServingFormat!=1 || captured.ModelDirectory()!="model" {t.Fatal("legacy plan was upgraded in place",captured)}
	if got:=f.stop(t);got.GetStatus()!="stopped" || got.GetApplyStatus()!="ready" {t.Fatal(got)}
	m.unavailable=true
	again,err:=f.s.CreateServingDeployment(ctx,req)
	if err!=nil || again.GetDeploymentId()!=r.GetDeploymentId() {t.Fatal("legacy idempotent replay failed",again,err)}
	f.cycle(t)
	if f.driver.creates!=1 || f.driver.starts!=1 {t.Fatal("stopped legacy deployment relaunched")}
}
