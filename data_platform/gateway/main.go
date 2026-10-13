package main

import (
	"data-platform/gateway/app"
	collectionv1 "data-platform/gen/collection/v1"
	operationv1 "data-platform/gen/operation/v1"
	refinementv1 "data-platform/gen/refinement/v1"
	"data-platform/internal/config"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"net/http"
	"time"
)

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
	api := app.New(collectionv1.NewCollectionServiceClient(col), operationv1.NewOperationServiceClient(op))
	api.Refinement = refinementv1.NewRefinementServiceClient(ref)
	address := config.Env("HTTP_ADDRESS", ":8000")
	server := &http.Server{Addr: address, Handler: api.Handler(), ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 120 * time.Second, WriteTimeout: 240 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10}
	log.Printf("data gateway REST listening on %s", address)
	log.Fatal(server.ListenAndServe())
}
