package app

import (
	"context"
	"strings"
	"testing"

	serving "model-platform/deployment/runtime"
	deploymentv1 "model-platform/gen/deployment/v1"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// Exercise the RPC boundary without PostgreSQL. Accepted profiles must reach
// the runtime-configuration check instead of being mistaken for OCR requests.
func TestCreateServingDeploymentProfileRoutingWithoutDatabase(t *testing.T) {
	for _,test := range []struct {
		name,engine,profile string
		code codes.Code
		message string
	}{
		{"llm", "vllm", "", codes.FailedPrecondition, "vLLM serving is not configured"},
		{"embedding", "vllm", serving.EmbeddingProfile, codes.FailedPrecondition, "vLLM serving is not configured"},
		{"reranker", "vllm", serving.RerankerProfile, codes.FailedPrecondition, "vLLM serving is not configured"},
		{"ocr", "vllm", serving.SuryaProfile, codes.InvalidArgument, "use CreateOCRDeployment"},
		{"unknown", "vllm", "unsupported-profile", codes.InvalidArgument, "unsupported serving profile"},
		{"demo", "demo", "", codes.InvalidArgument, "requires vLLM"},
	} {
		t.Run(test.name,func(t *testing.T) {
			service := &Service{}
			_,err := service.CreateServingDeployment(context.Background(), &deploymentv1.DeployModelRequest{
				ModelVersionId:"fb77d339-9d27-4832-ab20-d90093e2cd44",
				Serving:&deploymentv1.ServingSpec{Engine:test.engine,Profile:test.profile},
			})
			if status.Code(err)!=test.code || !strings.Contains(status.Convert(err).Message(),test.message) {
				t.Fatalf("profile reached the wrong creation path: %v",err)
			}
		})
	}
}
