package main

import (
	"context"
	"log"
	"os"
	"strconv"
	"time"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/grpcutil"
	"model-platform/internal/platformdb"
	"model-platform/internal/storage"
 "model-platform/internal/trainingoutput"
 "model-platform/internal/jobcontrol"
	trainingapp "model-platform/training/app"
	"model-platform/training/grpcclient"

	"google.golang.org/grpc"
)

func main() {
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	pool, err := platformdb.Connect(ctx, required("POSTGRES_URL"), "model_training")
	if err != nil {
		log.Fatal(err)
	}
	defer pool.Close()
	if err := trainingapp.InitSchema(ctx, pool); err != nil {
		log.Fatal(err)
	}
	clients, err := grpcclient.Connect(env("MANAGEMENT_GRPC_ADDRESS", "management:9000"), env("OPERATION_GRPC_ADDRESS", "operation:9000"))
	if err != nil {
		log.Fatal(err)
	}
	defer clients.Close()
	service := trainingapp.New(pool, clients)
	useSSL, err := strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil { log.Fatal(err) }
	store, err := storage.Connect(ctx, required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), env("DATASET_BUCKET", "model-datasets"), useSSL)
	if err != nil { log.Fatal(err) }
	service.ConfigureDatasets(store)
 archive,err:=jobcontrol.NewArchive(required("MINIO_ENDPOINT"),required("MINIO_ACCESS_KEY"),required("MINIO_SECRET_KEY"),env("GC_LOG_BUCKET","model-job-logs"),useSSL)
 if err!=nil{log.Fatal(err)}
 control:=jobcontrol.New(env("DISPATCHER_NODE_NAME",""),archive)
 service.ConfigureJobs(control)
 service.ConfigureOutputs(env("TRAINING_OUTPUT_ROOT",""),env("DISPATCHER_NODE_NAME",""),func(ctx context.Context)(trainingoutput.Store,error){
  return storage.ConnectExisting(ctx,required("MINIO_ENDPOINT"),required("MINIO_ACCESS_KEY"),required("MINIO_SECRET_KEY"),env("MINIO_BUCKET","model-artifacts"),useSSL)
 },control)
 service.ConfigureTracking(func(ctx context.Context,id string)error{return jobcontrol.CancelTracking(ctx,env("MLFLOW_TRACKING_URI","http://ops-mlflow:5000"),id)})
	go service.RunSubmissions(context.Background())
 go service.RunCancellations(context.Background())
 go service.RunOutputRecoveries(context.Background())
	address := env("GRPC_ADDRESS", ":9000")
	log.Printf("training gRPC listening on %s", address)
	log.Fatal(grpcutil.Serve(address, trainingv1.TrainingService_ServiceDesc.ServiceName, func(server *grpc.Server) { trainingv1.RegisterTrainingServiceServer(server, service) }))
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
