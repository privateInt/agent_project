package main

import (
	"context"
	app "data-platform/collection/app"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/blob"
	"data-platform/internal/config"
	"data-platform/internal/grpcutil"
	"data-platform/internal/platformdb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"time"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, e := platformdb.Connect(ctx, config.Required("POSTGRES_URL"), "data_collection")
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
	store, e := blob.Connect(ctx, config.Required("MINIO_ENDPOINT"), config.Required("MINIO_ACCESS_KEY"), config.Required("MINIO_SECRET_KEY"), config.Env("DATA_SOURCE_BUCKET", "data-sources"), config.Bool("MINIO_USE_SSL", false))
	if e != nil {
		log.Fatal(e)
	}
	service := app.New(pool, operationv1.NewOperationServiceClient(conn), store)
	refConn, e := grpc.NewClient(config.Env("REFINEMENT_GRPC_ADDRESS", "data-refinement:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer refConn.Close()
	service.SetRefinement(refinementv1.NewRefinementServiceClient(refConn))
	go service.RunSubmissions(context.Background())
	address := config.Env("GRPC_ADDRESS", ":9000")
	log.Printf("collection gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, collectionv1.CollectionService_ServiceDesc.ServiceName, func(server *grpc.Server) { collectionv1.RegisterCollectionServiceServer(server, service) }))
}
