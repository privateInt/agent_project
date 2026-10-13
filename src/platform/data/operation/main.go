package main

import (
	"context"
	"log"
	"os"
	"time"

	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/grpcutil"
	"data-platform/internal/platformdb"
	operationapp "data-platform/operation/app"
	"data-platform/operation/messaging"
 "data-platform/operation/outbox"

	"google.golang.org/grpc"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()

	pool, err := platformdb.Connect(ctx, required("POSTGRES_URL"), "data_operations")
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	if err := operationapp.InitSchema(ctx, pool); err != nil {
		log.Fatal(err)
	}

	if env("KAFKA_COMMAND_TOPIC", "data.operations.requested") != "data.operations.requested" { log.Fatal("Data Platform requires its own fixed topic") }
	publisher, err := messaging.Connect(ctx, required("KAFKA_BROKERS"), "data.operations.requested")
	if err != nil {
		log.Fatal(err)
	}
	defer publisher.Close()

	// The bootstrap context expires after 60s; never use it for the worker.
    workerCtx, stopWorker := context.WithCancel(context.Background())
    defer stopWorker()
    go outbox.New(pool, publisher).Run(workerCtx)
    service := &operationapp.ScopedService{Service:operationapp.New(pool)}
	address := env("GRPC_ADDRESS", ":9000")
	log.Printf("operation gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, operationv1.OperationService_ServiceDesc.ServiceName, func(server *grpc.Server) {
		operationv1.RegisterOperationServiceServer(server, service)
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
