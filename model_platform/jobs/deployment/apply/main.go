package main

import (
 "model-platform/internal/operationreport"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	deploymentv1 "model-platform/gen/deployment/v1"
	operationv1 "model-platform/gen/operation/v1"
	"os"
	"time"
)

func main() {
	// [DEPLOY-FLOW-04] One-shot deployment Job entrypoint selected by the common
	// Dispatcher for execution_type=deployment.apply. RESOURCE_ID is deployment_id.
    if env("EXECUTION_TYPE", "deployment.apply") == "deployment.serve" {
        runServing(); return
    }
	ctx := context.Background()
	operationID := required("OPERATION_ID")
	deploymentID := required("RESOURCE_ID")
	oc, err := grpc.NewClient(env("OPERATION_GRPC_ADDRESS", "operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	defer oc.Close()
	ops := operationreport.New(operationv1.NewOperationServiceClient(oc))
    if err := ops.Authorize(ctx,operationID,deploymentID,"deployment.apply"); err != nil { log.Printf("execution not authorized; process stopped: %v",err); return }
	dc, err := grpc.NewClient(env("DEPLOYMENT_GRPC_ADDRESS", "deployment:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		fail(ctx, ops, operationID, err.Error())
		return
	}
	defer dc.Close()
	deploy := deploymentv1.NewDeploymentServiceClient(dc)
	if _, err := ops.MarkRunning(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "applying"}); err != nil { log.Printf("job stopped: cannot mark operation running: %v", err); return }

	// [DEPLOY-FLOW-05] Legacy demo path, retained for control-plane smoke tests.
	// No model or inference server is loaded here. Real serving takes the
	// deployment.serve branch above and owns its GPU in a separate container.
	time.Sleep(2 * time.Second)
	// [DEPLOY-FLOW-06] Persist deployment=ready first; then mark the common
	// Operation succeeded/completed.
	if _, err := deploy.CompleteDeployment(ctx, &deploymentv1.CompleteDeploymentRequest{DeploymentId: deploymentID}); err != nil {
		fail(ctx, ops, operationID, err.Error())
		return
	}
	if _, err := ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "completed"}); err != nil { log.Printf("domain completed but operation completion report failed: %v", err); return }
	log.Printf("demo deployment completed deployment=%s", deploymentID)
}
func fail(ctx context.Context, ops operationv1.OperationServiceClient, id, message string) {
	_, _ = ops.FailOperation(ctx, &operationv1.FailOperationRequest{OperationId: id, Phase: "failed", ErrorCode: "DEPLOYMENT_JOB_FAILED", ErrorMessage: message, Retryable: false})
}
func required(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	log.Fatalf("%s is required", name)
	return ""
}
func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
