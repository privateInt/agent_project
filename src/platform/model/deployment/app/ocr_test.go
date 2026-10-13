package app

import (
	"context"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	serving "model-platform/deployment/runtime"
	commonv1 "model-platform/gen/common/v1"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	ocrv1 "model-platform/gen/ocr/v1"
	"testing"
)

type ocrManagement struct{ *servingManagement }

func (m *ocrManagement) GetModel(context.Context, *managementv1.GetModelRequest, ...grpc.CallOption) (*managementv1.Model, error) {
	return &managementv1.Model{ModelType: "ocr"}, nil
}
func (d *runtimeDriver) ProfileImage(ctx context.Context, profile string) (string, error) {
	return d.Image(ctx)
}

func TestOCRDeploymentGateIdentityAndStop(t *testing.T) {
	f := newServingFixture(t)
	management := f.s.clients.Management.(*servingManagement)
	management.version.RepoId = serving.SuryaRepository
	f.s.clients.Management = &ocrManagement{management}
	req := &deploymentv1.DeployModelRequest{ModelVersionId: f.sourceID, IdempotencyKey: uuid.NewString(), Serving: &deploymentv1.ServingSpec{Engine: "vllm", Profile: serving.SuryaProfile}, Resources: &commonv1.ExecutionResources{GpuIds: []int32{0}}}
	if _, e := f.s.CreateServingDeployment(context.Background(), req); status.Code(e) != codes.InvalidArgument {
		t.Fatal("legacy RPC accepted OCR", e)
	}
	created, e := f.s.CreateOCRDeployment(context.Background(), req)
	if e != nil {
		t.Fatal(e)
	}
	f.request.DeploymentId = created.DeploymentId
	f.request.OperationId = created.OperationId
	server := &OCRServer{Service: f.s}
	lookup := &ocrv1.ResolveDeploymentRequest{DeploymentId: created.DeploymentId}
	if _, e = server.ResolveDeployment(context.Background(), lookup); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("unready OCR returned", e)
	}
	f.driver.setHealthy(true)
	f.grant(t)
	f.cycle(t)
	f.cycle(t)
	identity, e := server.ResolveDeployment(context.Background(), lookup)
	if e != nil || identity.GetModelVersionId() != f.sourceID || identity.GetProfile() != serving.SuryaProfile || identity.GetRuntimeImageId() == "" || identity.GetSourceManifestSha256() == "" {
		t.Fatal(identity, e)
	}
	var engine string
	if e = f.s.pool.QueryRow(context.Background(), `SELECT engine FROM deployments WHERE deployment_id=$1`, created.DeploymentId).Scan(&engine); e != nil || engine != "vllm-surya" {
		t.Fatal("older controller could launch OCR with the LLM image", engine, e)
	}
	listed, e := f.s.ListDeployments(context.Background(), &deploymentv1.ListDeploymentsRequest{ModelVersionId: f.sourceID})
	if e != nil {
		t.Fatal(e)
	}
	found := false
	for _, d := range listed.Deployments {
		if d.DeploymentId == created.DeploymentId {
			found = d.GetServing().GetProfile() == serving.SuryaProfile && d.GetServing().GetEngine() == "vllm"
		}
	}
	if !found {
		t.Fatal("OCR profile missing from list")
	}
	f.stop(t)
	f.cycle(t)
	if _, e = server.ResolveDeployment(context.Background(), lookup); status.Code(e) != codes.FailedPrecondition {
		t.Fatal("stopped OCR returned", e)
	}
}
