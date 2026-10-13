package main

import (
	"context"
	"data-platform/dispatcher/executor"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/config"
	"data-platform/reconciler/app"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"time"
)

func cleanupDuration(key, fallback string) time.Duration {
	d, err := time.ParseDuration(config.Env(key, fallback))
	if err != nil || d < time.Second {
		log.Fatalf("%s must be a duration of at least 1s", key)
	}
	return d
}

func main() {
	op, e := grpc.NewClient(config.Env("OPERATION_GRPC_ADDRESS", "data-operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer op.Close()
	col, e := grpc.NewClient(config.Env("COLLECTION_GRPC_ADDRESS", "data-collection:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer col.Close()
	ref, e := grpc.NewClient(config.Env("REFINEMENT_GRPC_ADDRESS", "data-refinement:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if e != nil {
		log.Fatal(e)
	}
	defer ref.Close()
	cleanup := &app.JobCleanup{
		Operations: operationv1.NewOperationServiceClient(op), Call: executor.CLI,
		Mode: config.Env("DATA_JOB_CLEANUP_MODE", "apply"), Network: config.Env("JOB_NETWORK", "ops-infra"),
		SuccessKeep: cleanupDuration("DATA_JOB_SUCCESS_RETENTION", "1h"),
		FailureKeep: cleanupDuration("DATA_JOB_FAILURE_RETENTION", "168h"),
		Interval:    cleanupDuration("DATA_JOB_CLEANUP_INTERVAL", "5m"),
	}
	if err := cleanup.Validate(); err != nil {
		log.Fatal(err)
	}
	log.Printf("data job cleanup mode=%s success_retention=%s failure_retention=%s", cleanup.Mode, cleanup.SuccessKeep, cleanup.FailureKeep)
	go cleanup.Run(context.Background())
	log.Printf("data reconciler started: pre-start fencing and registered-result completion")
	app.Controller{Refinement: refinementv1.NewRefinementServiceClient(ref), Operations: operationv1.NewOperationServiceClient(op), Collection: collectionv1.NewCollectionServiceClient(col), Docker: executor.New("")}.Run(context.Background())
}
