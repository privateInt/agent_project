package app

import (
	"context"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	serving "model-platform/deployment/runtime"
	managementv1 "model-platform/gen/management/v1"
	"model-platform/internal/artifacts"
)

// Persist the exact base and adapter lineage. Never resolve a local training
// path from adapter_config.json, or select a base by repository name/latest.
func (s *Service) servingSource(ctx context.Context, mv *managementv1.ModelVersion, profile string) (serving.Source, error) {
	var source serving.Source
	base := mv
	if mv.GetArtifactKind() == "adapter" {
		id, err := uuid.Parse(mv.GetSourceModelVersionId())
		if err != nil || id == uuid.Nil || id.String() != mv.GetSourceModelVersionId() || id.String() == mv.GetModelVersionId() {
			return source, status.Error(codes.FailedPrecondition, "adapter requires a valid distinct base snapshot ID")
		}
		runID, err := uuid.Parse(mv.GetTrainingRunId())
		if err != nil || runID == uuid.Nil || mv.GetSourceType() != "training" ||
			!artifacts.ValidDigest(mv.GetArtifactManifestSha256()) || mv.GetArtifactSha256() != mv.GetArtifactManifestSha256() ||
			mv.GetArtifactSizeBytes() <= 0 || mv.GetArtifactFileCount() <= 0 || mv.GetBaseModelArtifactUri() == "" {
			return source, status.Error(codes.FailedPrecondition, "adapter metadata is not a verified training output")
		}
		base, err = s.clients.Management.GetModelVersion(ctx, &managementv1.GetModelVersionRequest{ModelVersionId: id.String()})
		if err != nil {
			return source, status.Error(codes.Unavailable, "adapter base snapshot lookup unavailable")
		}
		if base.GetModelVersionId() != id.String() || base.GetModelId() != mv.GetModelId() || base.GetArtifactUri() != mv.GetBaseModelArtifactUri() {
			return source, status.Error(codes.FailedPrecondition, "adapter base snapshot lineage mismatch")
		}
		source.Adapter = &serving.AdapterSource{
			ServingFormat:  serving.CurrentAdapterServingFormat,
			ModelVersionID: mv.GetModelVersionId(), TrainingRunID: mv.GetTrainingRunId(),
			URI: mv.GetArtifactUri(), SHA256: mv.GetArtifactManifestSha256(),
			SizeBytes: mv.GetArtifactSizeBytes(), FileCount: mv.GetArtifactFileCount(),
		}
	}
	if base.GetState() != "ready" || base.GetArtifactKind() != "snapshot" || base.GetSourceType() != "huggingface" ||
		!artifacts.ValidDigest(base.GetArtifactSha256()) || base.GetArtifactSizeBytes() <= 0 || base.GetArtifactUri() == "" {
		return serving.Source{}, status.Error(codes.FailedPrecondition, "vLLM requires a verified ready Hugging Face base snapshot")
	}
	model, err := s.clients.Management.GetModel(ctx, &managementv1.GetModelRequest{ModelId: base.GetModelId()})
	if err != nil {
		return serving.Source{}, status.Error(codes.Unavailable, "source model metadata unavailable")
	}
	if err := serving.ValidateModelProfile(profile, model.GetModelType(), base.GetRepoId(), mv.GetArtifactKind()); err != nil {
		return serving.Source{}, status.Error(codes.FailedPrecondition, err.Error())
	}
	source.ModelVersionID, source.URI = base.GetModelVersionId(), base.GetArtifactUri()
	source.SHA256, source.SizeBytes = base.GetArtifactSha256(), base.GetArtifactSizeBytes()
	return source, nil
}
