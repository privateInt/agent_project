package main

import (
    "context"
    "testing"

    deploymentv1 "model-platform/gen/deployment/v1"
    operationv1 "model-platform/gen/operation/v1"
    "google.golang.org/grpc"
)

type servingDomainClient struct {
    deploymentv1.DeploymentServiceClient
    result *deploymentv1.Deployment
}
func (c servingDomainClient) GetDeployment(context.Context, *deploymentv1.GetDeploymentRequest, ...grpc.CallOption) (*deploymentv1.Deployment, error) {
    return c.result, nil
}

func TestServingCompletionSurvivesRuntimeStop(t *testing.T) {
    op := &operationv1.Operation{OperationId: "operation", ResourceId: "deployment", ResourceType: "deployment", ExecutionType: "deployment.serve"}
    result := &deploymentv1.Deployment{DeploymentId: "deployment", OperationId: "operation", ApplyStatus: "ready"}
    w := worker{deployment: servingDomainClient{result: result}}
    for _, state := range []string{"ready", "stopping", "stopped", "unhealthy", "failed"} {
        result.Status = state
        got, err := w.domain(context.Background(), op)
        if err != nil || got != "succeeded" { t.Fatalf("runtime %s changed provisioning receipt: %s %v", state, got, err) }
    }
    result.ApplyStatus, result.Status = "applying", "ready"
    if got, err := w.domain(context.Background(), op); err != nil || got != "unknown" { t.Fatal("runtime state replaced the apply receipt", got, err) }
    result.ApplyStatus = "failed"
    if got, err := w.domain(context.Background(), op); err != nil || got != "failed" { t.Fatal(got, err) }
    result.OperationId = "other"
    if _, err := w.domain(context.Background(), op); err == nil { t.Fatal("foreign operation receipt accepted") }
}
