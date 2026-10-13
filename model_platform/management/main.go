package main

import (
	"context"
	"log"
	"os"
	"strconv"
	"time"

	"google.golang.org/grpc/credentials/insecure"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/grpcutil"
	"model-platform/internal/platformdb"
	"model-platform/internal/storage"
	managementapp "model-platform/management/app"
	"model-platform/management/grpcclient"

	"google.golang.org/grpc"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, err := platformdb.Connect(ctx, required("POSTGRES_URL"), "model_management")
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	if err := managementapp.InitSchema(ctx, pool); err != nil {
		log.Fatal(err)
	}
	ops, err := grpcclient.ConnectOperation(env("OPERATION_GRPC_ADDRESS", "operation:9000"))
	if err != nil {
		log.Fatal(err)
	}
	defer ops.Close()
	// Platform-owned key: this is not a Hugging Face token. It only encrypts
	// short-lived user credentials stored by Management.
	credentialKey, err := managementapp.ParseCredentialEncryptionKey(required("CREDENTIAL_ENCRYPTION_KEY"))
	if err != nil {
		log.Fatal(err)
	}
	useSSL, err := strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil {
		log.Fatal(err)
	}
	artifactStore, err := storage.ConnectExisting(ctx, required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), env("MINIO_BUCKET", "model-artifacts"), useSSL)
	if err != nil {
		log.Fatal(err)
	}
	service := managementapp.New(pool, ops, credentialKey, artifactStore)
	dep, e := grpc.NewClient(env("DEPLOYMENT_GRPC_ADDRESS", "deployment:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer dep.Close()
	train, e := grpc.NewClient(env("TRAINING_GRPC_ADDRESS", "training:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer train.Close()
	service.SetPurgeServices(deploymentv1.NewDeploymentServiceClient(dep), trainingv1.NewTrainingServiceClient(train))
	go service.RunSubmissions(context.Background())
	address := env("GRPC_ADDRESS", ":9000")
	log.Printf("management gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, managementv1.ManagementService_ServiceDesc.ServiceName, func(server *grpc.Server) { managementv1.RegisterManagementServiceServer(server, service) }))
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
