package main
import(
 "embed";"io/fs";"log";"net/http";"time"
 "knowledge-platform/gateway/app"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 collectionv1 "knowledge-platform/gen/data/collection/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/config"
 "google.golang.org/grpc";"google.golang.org/grpc/credentials/insecure"
)
//go:embed web/*
var web embed.FS
func main(){
 connect:=func(key,fallback string)*grpc.ClientConn{c,e:=grpc.NewClient(config.Env(key,fallback),grpc.WithTransportCredentials(insecure.NewCredentials()));if e!=nil{log.Fatal(e)};return c}
 k:=connect("KNOWLEDGE_GRPC_ADDRESS","knowledge-service:9000");defer k.Close()
 o:=connect("OPERATION_GRPC_ADDRESS","knowledge-operation:9000");defer o.Close()
 c:=connect("DATA_COLLECTION_GRPC_ADDRESS","data-collection:9000");defer c.Close()
 r:=connect("DATA_REFINEMENT_GRPC_ADDRESS","data-refinement:9000");defer r.Close()
 files,e:=fs.Sub(web,"web");if e!=nil{log.Fatal(e)}
 modelConsole,e:=app.NewModelConsole(config.Env("MODEL_GATEWAY_URL","http://model-gateway:8000"));if e!=nil{log.Fatal(e)}
 api:=&app.Server{Indexes:indexingv1.NewIndexingServiceClient(k),Knowledge:chunkingv1.NewChunkingServiceClient(k),Operations:operationv1.NewOperationServiceClient(o),Collection:collectionv1.NewCollectionServiceClient(c),Refinement:refinementv1.NewRefinementServiceClient(r),Console:http.FileServer(http.FS(files)),ModelConsole:modelConsole}
 server:=&http.Server{Addr:config.Env("HTTP_ADDRESS",":8000"),Handler:api.Handler(),ReadHeaderTimeout:5*time.Second,ReadTimeout:110*time.Second,WriteTimeout:110*time.Second,IdleTimeout:60*time.Second,MaxHeaderBytes:32<<10}
 log.Fatal(server.ListenAndServe())
}
