package trainingoutput

import (
 "context"
 "fmt"
 "log"
 managementv1 "model-platform/gen/management/v1"
 "model-platform/internal/artifacts"
)

type Store interface {
 URI(string) string
 UploadManifest(context.Context,string,string,[]byte) error
 VerifyManifest(context.Context,string,string)(artifacts.Manifest,error)
}

// Publish never trains. Both the original Job and a recovery request use the
// same immutable reservation, manifest-last upload and server-side readback.
func Publish(ctx context.Context, management managementv1.ManagementServiceClient, store Store, trainingRunID, sourceID, root string, manifest artifacts.Manifest, encoded []byte, digest string, phase func(string)error) (*managementv1.ModelVersion,error) {
 if phase==nil {phase=func(string)error{return ctx.Err()}}
	if err := phase("registering_training_output"); err != nil { return nil, err }
	reserved, err := management.RegisterTrainingOutput(ctx, &managementv1.RegisterTrainingOutputRequest{TrainingRunId: trainingRunID, SourceModelVersionId: sourceID, ManifestSha256: digest})
	if err != nil {
		return nil, err
	}
	if reserved.GetTrainingRunId()!=trainingRunID || reserved.GetSourceModelVersionId()!=sourceID || reserved.GetArtifactKind()!="adapter" || reserved.GetArtifactManifestSha256()!=digest {return nil,fmt.Errorf("output reservation identity mismatch")}
	if reserved.GetState() == "ready" {
		// Never overwrite a completed version. It must match the exact input.
		if reserved.GetArtifactManifestSha256() != digest {
			return nil, fmt.Errorf("ready output has a different manifest")
		}
		prefix, err := artifacts.Prefix(reserved.GetModelId(), reserved.GetModelVersionId(), digest)
		if err != nil {
			return nil, err
		}
		verified, err := store.VerifyManifest(ctx, prefix, digest)
		if err != nil {
			return nil, err
		}
		if verified.FileCount != manifest.FileCount || verified.TotalBytes != manifest.TotalBytes || reserved.GetArtifactUri()!=store.URI(prefix) || reserved.GetArtifactFileCount()!=int64(manifest.FileCount) || reserved.GetArtifactSizeBytes()!=manifest.TotalBytes {
			return nil, fmt.Errorf("ready output differs from local manifest")
		}
		return reserved, nil
	}
	if reserved.GetState() != "importing" {
		return nil, fmt.Errorf("output reservation state is %s", reserved.GetState())
	}
	published := false
	defer func() {
		if !published {
			if _, e := management.FailTrainingOutput(ctx, &managementv1.FailTrainingOutputRequest{ModelVersionId: reserved.GetModelVersionId()}); e != nil {
				log.Printf("fail output reservation: %v", e)
			}
		}
	}()
	prefix, err := artifacts.Prefix(reserved.GetModelId(), reserved.GetModelVersionId(), digest)
	if err != nil {
		return nil, err
	}
	uri := store.URI(prefix)
	if err := phase("uploading_training_artifact"); err != nil { return nil, err }
	if err = store.UploadManifest(ctx, root, prefix, encoded); err != nil {
		return nil, err
	}
	if err := phase("verifying_training_artifact"); err != nil { return nil, err }
	verified, err := store.VerifyManifest(ctx, prefix, digest)
	if err != nil {
		return nil, err
	}
	if verified.FileCount != manifest.FileCount || verified.TotalBytes != manifest.TotalBytes {
		return nil, fmt.Errorf("verified artifact differs from local manifest")
	}
	if err := phase("registering_training_model_version"); err != nil { return nil, err }
	result, err := management.CompleteTrainingOutput(ctx, &managementv1.CompleteTrainingOutputRequest{ModelVersionId: reserved.GetModelVersionId(), ArtifactUri: uri, ManifestSha256: digest, ArtifactSizeBytes: verified.TotalBytes, ArtifactFileCount: int64(verified.FileCount)})
	if err != nil {
		return nil, err
	}
	if result.GetState() != "ready" || result.GetArtifactUri() != uri || result.GetModelVersionId()!=reserved.GetModelVersionId() || result.GetTrainingRunId()!=trainingRunID || result.GetSourceModelVersionId()!=sourceID || result.GetArtifactKind()!="adapter" || result.GetArtifactManifestSha256()!=digest || result.GetArtifactFileCount()!=int64(manifest.FileCount) || result.GetArtifactSizeBytes()!=manifest.TotalBytes {
		return nil, fmt.Errorf("Management did not return the verified ready output")
	}
	published = true
	return result, nil
}
