package main
import(
 "context";"log";"time"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 embeddingv1 "knowledge-platform/gen/model/embedding/v1"
 rerankingv1 "knowledge-platform/gen/model/reranking/v1"
 "knowledge-platform/internal/searchstore"
 collectionv1 "knowledge-platform/gen/data/collection/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/blob";"knowledge-platform/internal/config";"knowledge-platform/internal/grpcutil";"knowledge-platform/internal/platformdb"
 dataclient "knowledge-platform/internal/clients/data"
 "knowledge-platform/service/app"
 "google.golang.org/grpc";"google.golang.org/grpc/credentials/insecure"
)
func main(){
 ctx,cancel:=context.WithTimeout(context.Background(),60*time.Second);defer cancel()
 pool,e:=platformdb.Connect(ctx,config.Required("POSTGRES_URL"),"knowledge_catalog");if e!=nil{log.Fatal(e)};defer pool.Close()
 if e=app.InitSchema(ctx,pool);e!=nil{log.Fatal(e)}
 connect:=func(key,fallback string)*grpc.ClientConn{c,e:=grpc.NewClient(config.Env(key,fallback),grpc.WithTransportCredentials(insecure.NewCredentials()));if e!=nil{log.Fatal(e)};return c}
 op:=connect("OPERATION_GRPC_ADDRESS","knowledge-operation:9000");defer op.Close()
 col:=connect("DATA_COLLECTION_GRPC_ADDRESS","data-collection:9000");defer col.Close()
 ref:=connect("DATA_REFINEMENT_GRPC_ADDRESS","data-refinement:9000");defer ref.Close()
 store,e:=blob.Connect(ctx,config.Required("MINIO_ENDPOINT"),config.Required("MINIO_ACCESS_KEY"),config.Required("MINIO_SECRET_KEY"),config.Bool("MINIO_USE_SSL",false));if e!=nil{log.Fatal(e)}
 service:=app.New(pool,operationv1.NewOperationServiceClient(op),dataclient.Client{Collection:collectionv1.NewCollectionServiceClient(col),Refinement:refinementv1.NewRefinementServiceClient(ref)},store)
 model:=connect("MODEL_EMBEDDING_GRPC_ADDRESS","model-deployment:9000");defer model.Close()
 reranker:=connect("MODEL_RERANKING_GRPC_ADDRESS","model-deployment:9000");defer reranker.Close()
 var search app.IndexStore
 if config.Bool("KNOWLEDGE_INDEXING_ENABLED",false){client,err:=searchstore.New(config.Required("OPENSEARCH_ENDPOINT"),config.Required("OPENSEARCH_USERNAME"),config.Required("OPENSEARCH_PASSWORD"),config.Required("OPENSEARCH_CA_BASE64"));if err!=nil{log.Fatal(err)};search=client}
 indexes:=app.NewIndexes(service,embeddingv1.NewEmbeddingServiceClient(model),refinementv1.NewRefinementServiceClient(ref),search,rerankingv1.NewRerankingServiceClient(reranker))
 go indexes.RunIndexMaintenance(context.Background())
 go service.RunSubmissions(context.Background())
 log.Fatal(grpcutil.Serve(config.Env("GRPC_ADDRESS",":9000"),chunkingv1.ChunkingService_ServiceDesc.ServiceName,func(s *grpc.Server){chunkingv1.RegisterChunkingServiceServer(s,service);indexingv1.RegisterIndexingServiceServer(s,indexes)}))
}
