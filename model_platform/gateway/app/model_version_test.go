package app

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"model-platform/gateway/grpcclient"
	managementv1 "model-platform/gen/management/v1"

	"github.com/gofiber/fiber/v3"
	"google.golang.org/grpc"
)

type modelVersionRouteClient struct {
	managementv1.ManagementServiceClient
	version     *managementv1.ModelVersion
	requestedID string
}

func (c *modelVersionRouteClient) GetModelVersion(_ context.Context, req *managementv1.GetModelVersionRequest, _ ...grpc.CallOption) (*managementv1.ModelVersion, error) {
	c.requestedID = req.GetModelVersionId()
	return c.version, nil
}

// Exercise the real HTTP route and JSON serialization. The smoke's fake
// responses cannot detect a field omitted by the Gateway's protobuf mapping.
func TestModelVersionRESTPreservesSourceAndLineage(t *testing.T) {
	for _, tc := range []struct{ source, kind string }{
		{"training", "adapter"},
		{"huggingface", "snapshot"},
		{"upload", "snapshot"},
	} {
		t.Run(tc.source, func(t *testing.T) {
			version := &managementv1.ModelVersion{
				ModelVersionId: "80e34e1f-f5da-4448-a0ca-282e76d78526",
				ModelId:        "06b6555f-c939-49cc-ac86-5083e41aa13c",
				State:          "ready",
				SourceType:     tc.source,
				ArtifactKind:   tc.kind,
			}
			if tc.source == "training" {
				version.TrainingRunId = "efa680aa-c507-4762-982f-e81eb1364c1e"
				version.SourceModelVersionId = "cceae526-7b7c-47d4-870b-117372b51885"
				version.BaseModelArtifactUri = "s3://model-artifacts/base/"
			}
			client := &modelVersionRouteClient{version: version}
			app := fiber.New()
			New(&grpcclient.Clients{Management: client}).Register(app)
			request := httptest.NewRequest(http.MethodGet, "/api/model-versions/"+version.ModelVersionId, nil)
			response, err := app.Test(request)
			if err != nil {
				t.Fatal(err)
			}
			defer response.Body.Close()
			if response.StatusCode != http.StatusOK || client.requestedID != version.ModelVersionId {
				t.Fatal("model version GET did not reach Management", response.StatusCode, client.requestedID)
			}
			var body map[string]any
			if err := json.NewDecoder(response.Body).Decode(&body); err != nil {
				t.Fatal(err)
			}
			for key, expected := range map[string]string{
				"model_version_id":        version.ModelVersionId,
				"model_id":                version.ModelId,
				"state":                   "ready",
				"source_type":             tc.source,
				"artifact_kind":           tc.kind,
				"training_run_id":         version.TrainingRunId,
				"source_model_version_id": version.SourceModelVersionId,
				"base_model_artifact_uri": version.BaseModelArtifactUri,
			} {
				if value, exists := body[key]; !exists || value != expected {
					t.Fatalf("REST field %s: got %#v, present=%v, want %q", key, value, exists, expected)
				}
			}
		})
	}
}
