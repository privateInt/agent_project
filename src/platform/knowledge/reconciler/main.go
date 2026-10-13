package main
import(
 "context";"log";"time"
 "knowledge-platform/dispatcher/executor"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/config";"knowledge-platform/reconciler/app"
 "google.golang.org/grpc";"google.golang.org/grpc/credentials/insecure"
)
func duration(key,fallback string)time.Duration{v,e:=time.ParseDuration(config.Env(key,fallback));if e!=nil||v<time.Second{log.Fatalf("invalid duration: %s",key)};return v}
func main(){
 op,e:=grpc.NewClient(config.Env("OPERATION_GRPC_ADDRESS","knowledge-operation:9000"),grpc.WithTransportCredentials(insecure.NewCredentials()));if e!=nil{log.Fatal(e)};defer op.Close()
 domain,e:=grpc.NewClient(config.Env("KNOWLEDGE_GRPC_ADDRESS","knowledge-service:9000"),grpc.WithTransportCredentials(insecure.NewCredentials()));if e!=nil{log.Fatal(e)};defer domain.Close()
 ops:=operationv1.NewOperationServiceClient(op)
 cleanup:=&app.JobCleanup{Operations:ops,Call:executor.CLI,Mode:config.Env("KNOWLEDGE_JOB_CLEANUP_MODE","apply"),Network:config.Env("JOB_NETWORK","ops-infra"),SuccessKeep:duration("KNOWLEDGE_JOB_SUCCESS_RETENTION","1h"),FailureKeep:duration("KNOWLEDGE_JOB_FAILURE_RETENTION","168h"),Interval:duration("KNOWLEDGE_JOB_CLEANUP_INTERVAL","5m")}
 if e=cleanup.Validate();e!=nil{log.Fatal(e)};go cleanup.Run(context.Background())
 app.Controller{Operations:ops,Knowledge:chunkingv1.NewChunkingServiceClient(domain),Docker:executor.New(""),Indexes:indexingv1.NewIndexingServiceClient(domain)}.Run(context.Background())
}
