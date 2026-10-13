package main

import (
	"context"
	"log"
	serving "model-platform/deployment/runtime"
	"model-platform/internal/storage"
	"os"
	"strconv"
	"time"

	deploymentapp "model-platform/deployment/app"
	"model-platform/deployment/grpcclient"
	deploymentv1 "model-platform/gen/deployment/v1"
	ocrv1 "model-platform/gen/ocr/v1"
    embeddingv1 "model-platform/gen/embedding/v1"
    "model-platform/internal/embedding"
    "model-platform/internal/reranking"
    rerankingv1 "model-platform/gen/reranking/v1"
	"model-platform/internal/grpcutil"
	"model-platform/internal/platformdb"

	"google.golang.org/grpc"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, err := platformdb.Connect(ctx, required("POSTGRES_URL"), "model_deployment")
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	if err := deploymentapp.InitSchema(ctx, pool); err != nil {
		log.Fatal(err)
	}
	clients, err := grpcclient.Connect(env("MANAGEMENT_GRPC_ADDRESS", "management:9000"), env("OPERATION_GRPC_ADDRESS", "operation:9000"))
	if err != nil {
		log.Fatal(err)
	}
	defer clients.Close()
	service := deploymentapp.New(pool, clients)
	config := serving.Config{OCRImage: env("SURYA_VLLM_IMAGE", serving.SuryaImage), Image: env("VLLM_IMAGE", "vllm/vllm-openai:v0.15.0"), Network: env("SERVING_NETWORK", "ops-infra"), Volume: env("SERVING_CACHE_VOLUME", "model-serving-cache"), CacheDir: "/serving-cache", NodeName: os.Getenv("DISPATCHER_NODE_NAME")}
	useSSL, err := strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil {
		log.Fatal(err)
	}
	endpoint, accessKey, secretKey, bucket := required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), env("MINIO_BUCKET", "model-artifacts")
	fetcher := newServingFetcher(config.CacheDir, func(ctx context.Context) (serving.ArtifactStore, error) {
		return storage.ConnectExisting(ctx, endpoint, accessKey, secretKey, bucket, useSSL)
	})
	if err := service.ConfigureServing(config, serving.Docker{Config: config}, fetcher); err != nil {
		log.Fatal(err)
	}
	go service.RunServing(context.Background())
	go service.RunSubmissions(context.Background())
	address := env("GRPC_ADDRESS", ":9000")
	log.Printf("deployment gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, deploymentv1.DeploymentService_ServiceDesc.ServiceName, func(server *grpc.Server) {
		deploymentv1.RegisterDeploymentServiceServer(server, service)
		ocrv1.RegisterOCRDeploymentServiceServer(server, &deploymentapp.OCRServer{Service: service})
        embeddingv1.RegisterEmbeddingServiceServer(server, &deploymentapp.EmbeddingServer{Service: service, HTTP: embedding.NewHTTPClient()})
        rerankingv1.RegisterRerankingServiceServer(server, &deploymentapp.RerankingServer{Service: service, HTTP: reranking.NewHTTPClient()})
	}))
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
