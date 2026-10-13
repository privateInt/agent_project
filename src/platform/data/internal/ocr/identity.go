// Package ocr owns the pinned contract for calling an already deployed model.
package ocr

import (
	"context"
	ocrv1 "data-platform/gen/ocr/v1"
	"encoding/json"
	"fmt"
	"github.com/google/uuid"
	"regexp"
)

const Profile = "surya-ocr-v2"
const Processor = "cpu-parser-surya-v1"
const ClientVersion = "surya-openai-v1"

type Identity struct {
	DeploymentID       string `json:"deployment_id"`
	ModelVersionID     string `json:"model_version_id"`
	RuntimeImageID     string `json:"runtime_image_id"`
	RuntimeContainerID string `json:"runtime_container_id"`
	Endpoint           string `json:"internal_endpoint"`
	Model              string `json:"served_model_name"`
	Profile            string `json:"profile"`
	ManifestSHA256     string `json:"source_manifest_sha256"`
}

func (i *Identity) Validate() error {
	if i == nil {
		return fmt.Errorf("missing OCR identity")
	}
	for _, value := range []string{i.DeploymentID, i.ModelVersionID} {
		id, e := uuid.Parse(value)
		if e != nil || id == uuid.Nil || id.String() != value {
			return fmt.Errorf("invalid OCR identity")
		}
	}
	hex := regexp.MustCompile(`^[a-f0-9]{64}$`)
	if i.Profile != Profile || i.Endpoint != "http://model-serving-"+i.DeploymentID+":8000/v1" || i.Model != "deployment-"+i.DeploymentID || !hex.MatchString(i.RuntimeContainerID) || !hex.MatchString(i.ManifestSHA256) || !regexp.MustCompile(`^sha256:[a-f0-9]{64}$`).MatchString(i.RuntimeImageID) {
		return fmt.Errorf("OCR deployment identity mismatch")
	}
	return nil
}

func Decode(raw string) (*Identity, error) {
	if raw == "" {
		return nil, nil
	}
	var i Identity
	if json.Unmarshal([]byte(raw), &i) != nil {
		return nil, fmt.Errorf("invalid stored OCR identity")
	}
	return &i, i.Validate()
}

func Resolve(ctx context.Context, client ocrv1.OCRDeploymentServiceClient, id string) (*Identity, error) {
	if client == nil {
		return nil, fmt.Errorf("OCR discovery is not configured")
	}
	d, e := client.ResolveDeployment(ctx, &ocrv1.ResolveDeploymentRequest{DeploymentId: id})
	if e != nil {
		return nil, e
	}
	i := &Identity{d.GetDeploymentId(), d.GetModelVersionId(), d.GetRuntimeImageId(), d.GetRuntimeContainerId(), d.GetInternalEndpoint(), d.GetServedModelName(), d.GetProfile(), d.GetSourceManifestSha256()}
	if i.DeploymentID != id {
		return nil, fmt.Errorf("OCR deployment identity changed")
	}
	return i, i.Validate()
}
