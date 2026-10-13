package main

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	ocrv1 "data-platform/gen/ocr/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/config"
	"data-platform/internal/grpcutil"
	"data-platform/internal/platformdb"
	app "data-platform/refinement/app"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"time"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, e := platformdb.Connect(ctx, config.Required("POSTGRES_URL"), "data_refinement")
	if e != nil {
		log.Fatal(e)
	}
	defer pool.Close()
	if e = app.InitSchema(ctx, pool); e != nil {
		log.Fatal(e)
	}
	conn, e := grpc.NewClient(config.Env("OPERATION_GRPC_ADDRESS", "data-operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer conn.Close()
	store, e := blob.ConnectCanonical(ctx, config.Required("MINIO_ENDPOINT"), config.Required("MINIO_ACCESS_KEY"), config.Required("MINIO_SECRET_KEY"), config.Bool("MINIO_USE_SSL", false))
	if e != nil {
		log.Fatal(e)
	}
	col, e := grpc.NewClient(config.Env("COLLECTION_GRPC_ADDRESS", "data-collection:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer col.Close()
	service := app.New(pool, operationv1.NewOperationServiceClient(conn), collectionv1.NewCollectionServiceClient(col), store)
	ocrConn, e := grpc.NewClient(config.Env("MODEL_DEPLOYMENT_GRPC_ADDRESS", "model-deployment:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer ocrConn.Close()
	service.ConfigureOCR(ocrv1.NewOCRDeploymentServiceClient(ocrConn))
	go service.RunSubmissions(context.Background())
	address := config.Env("GRPC_ADDRESS", ":9000")
	log.Printf("refinement gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, refinementv1.RefinementService_ServiceDesc.ServiceName, func(server *grpc.Server) { refinementv1.RegisterRefinementServiceServer(server, service) }))
}
