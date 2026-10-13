package app

import (
	"context"
	"encoding/json"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	serving "model-platform/deployment/runtime"
	deploymentv1 "model-platform/gen/deployment/v1"
	ocrv1 "model-platform/gen/ocr/v1"
	"regexp"
	"time"
)

func (s *Service) CreateOCRDeployment(ctx context.Context, req *deploymentv1.DeployModelRequest) (*deploymentv1.DeployModelResponse, error) {
	spec, err := serving.Normalize(req.GetServing())
	if err != nil || spec.Profile != serving.SuryaProfile {
		return nil, status.Error(codes.InvalidArgument, "CreateOCRDeployment requires the surya-ocr-v2 profile")
	}
	return s.createDeployment(ctx, req)
}

type OCRServer struct {
	ocrv1.UnimplementedOCRDeploymentServiceServer
	Service *Service
}

// Return only a ready, owned Surya runtime. Data never reads the model DB or
// supplies an arbitrary inference endpoint, image, or model name.
func (s *OCRServer) ResolveDeployment(ctx context.Context, req *ocrv1.ResolveDeploymentRequest) (*ocrv1.DeploymentIdentity, error) {
	id, err := deploymentID(req.GetDeploymentId())
	if err != nil || id != req.GetDeploymentId() {
		return nil, status.Error(codes.InvalidArgument, "canonical deployment UUID required")
	}
	dep, err := s.Service.GetDeployment(ctx, &deploymentv1.GetDeploymentRequest{DeploymentId: id})
	if err != nil {
		return nil, err
	}
	if dep.Status != "ready" || dep.RuntimeState != "ready" || dep.DesiredState != "running" || dep.GetServing().GetProfile() != serving.SuryaProfile || dep.GetServing().GetEngine() != "vllm" || dep.ArtifactKind != "snapshot" {
		return nil, status.Error(codes.FailedPrecondition, "OCR deployment is not ready")
	}
	if dep.RuntimeCheckedAt == nil || time.Since(dep.RuntimeCheckedAt.AsTime()) > time.Minute {
		return nil, status.Error(codes.Unavailable, "OCR runtime observation is stale")
	}
	var encoded []byte
	if err = s.Service.pool.QueryRow(ctx, `SELECT serving_source FROM deployments WHERE deployment_id=$1 AND engine='vllm-surya'`, id).Scan(&encoded); err != nil {
		return nil, status.Error(codes.Unavailable, "OCR source identity unavailable")
	}
	var source serving.Source
	digest := regexp.MustCompile(`^[a-f0-9]{64}$`)
	if json.Unmarshal(encoded, &source) != nil || source.Adapter != nil || source.ModelVersionID != dep.ModelVersionId || !digest.MatchString(source.SHA256) || !digest.MatchString(dep.RuntimeContainerId) || !regexp.MustCompile(`^sha256:[a-f0-9]{64}$`).MatchString(dep.RuntimeImageId) || dep.InternalEndpoint != "http://model-serving-"+id+":8000/v1" || dep.ServedModelName != "deployment-"+id {
		return nil, status.Error(codes.FailedPrecondition, "OCR runtime identity mismatch")
	}
	return &ocrv1.DeploymentIdentity{DeploymentId: id, ModelVersionId: dep.ModelVersionId, RuntimeImageId: dep.RuntimeImageId, RuntimeContainerId: dep.RuntimeContainerId, InternalEndpoint: dep.InternalEndpoint, ServedModelName: dep.ServedModelName, Profile: serving.SuryaProfile, SourceManifestSha256: source.SHA256}, nil
}
