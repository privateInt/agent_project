package app

import (
	"context"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gofiber/fiber/v3"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"model-platform/gateway/grpcclient"
	deploymentv1 "model-platform/gen/deployment/v1"
)

type servingRouteClient struct {
	deploymentv1.DeploymentServiceClient
	serving, legacy int
	oldServer       bool
	key             string
}

func (c *servingRouteClient) CreateServingDeployment(_ context.Context, r *deploymentv1.DeployModelRequest, _ ...grpc.CallOption) (*deploymentv1.DeployModelResponse, error) {
	c.serving++
	c.key = r.GetIdempotencyKey()
	if c.oldServer {
		return nil, status.Error(codes.Unimplemented, "old server")
	}
	return &deploymentv1.DeployModelResponse{DeploymentId: "serving", OperationId: "operation", Status: "pending"}, nil
}
func (c *servingRouteClient) DeployModel(context.Context, *deploymentv1.DeployModelRequest, ...grpc.CallOption) (*deploymentv1.DeployModelResponse, error) {
	c.legacy++
	return &deploymentv1.DeployModelResponse{DeploymentId: "demo"}, nil
}
func TestServingRouteNeverFallsBackToDemo(t *testing.T) {
	client := &servingRouteClient{}
	app := fiber.New()
	New(&grpcclient.Clients{Deployment: client}).Register(app)
	call := func(body string) int {
		req := httptest.NewRequest("POST", "/api/deployments", strings.NewReader(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Idempotency-Key", "same-key")
		res, err := app.Test(req)
		if err != nil {
			t.Fatal(err)
		}
		defer res.Body.Close()
		return res.StatusCode
	}
	if code := call(`{"serving":{"engine":"vllm"}}`); code != 202 || client.serving != 1 || client.legacy != 0 || client.key != "same-key" {
		t.Fatal("wrong real-serving route", code)
	}
	client.oldServer = true
	if code := call(`{"serving":{"engine":"vllm"}}`); code == 202 || client.legacy != 0 {
		t.Fatal("old server silently created a demo", code)
	}
	if code := call(`{}`); code != 202 || client.legacy != 1 {
		t.Fatal("legacy demo compatibility changed", code)
	}
}

type ocrRouteClient struct {
	*servingRouteClient
	ocr     int
	profile string
}

func (c *ocrRouteClient) CreateOCRDeployment(_ context.Context, r *deploymentv1.DeployModelRequest, _ ...grpc.CallOption) (*deploymentv1.DeployModelResponse, error) {
	c.ocr++
	c.profile = r.GetServing().GetProfile()
	return nil, status.Error(codes.Unimplemented, "old OCR server")
}
func TestOCRRouteNeverFallsBackToTextServing(t *testing.T) {
	client := &ocrRouteClient{servingRouteClient: &servingRouteClient{}}
	app := fiber.New()
	New(&grpcclient.Clients{Deployment: client}).Register(app)
	req := httptest.NewRequest("POST", "/api/deployments", strings.NewReader(`{"serving":{"engine":"vllm","profile":"surya-ocr-v2"}}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Idempotency-Key", "ocr-key")
	res, e := app.Test(req)
	if e != nil {
		t.Fatal(e)
	}
	defer res.Body.Close()
	if res.StatusCode == 202 || client.ocr != 1 || client.serving != 0 || client.legacy != 0 || client.profile != "surya-ocr-v2" {
		t.Fatal("OCR request downgraded to another serving mode")
	}
}
