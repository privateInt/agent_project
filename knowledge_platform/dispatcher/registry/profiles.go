package registry
import("fmt";"knowledge-platform/internal/config";"knowledge-platform/internal/execution")
type Registry struct{}
func(Registry)Get(kind string)(execution.Profile,error){
 if kind=="indexing.build"{return execution.Profile{Image:config.Env("INDEXING_JOB_IMAGE","knowledge-platform-indexing-job:dev"),ForwardEnv:[]string{"OPERATION_GRPC_ADDRESS","KNOWLEDGE_GRPC_ADDRESS","MODEL_EMBEDDING_GRPC_ADDRESS"}},nil}
 if kind!="chunking.run"{return execution.Profile{},fmt.Errorf("unsupported execution type")}
 return execution.Profile{Image:config.Env("CHUNKING_JOB_IMAGE","knowledge-platform-chunking-job:dev"),ForwardEnv:[]string{"OPERATION_GRPC_ADDRESS","KNOWLEDGE_GRPC_ADDRESS","DATA_COLLECTION_GRPC_ADDRESS","DATA_REFINEMENT_GRPC_ADDRESS","MINIO_ENDPOINT","MINIO_ACCESS_KEY","MINIO_SECRET_KEY","MINIO_USE_SSL"}},nil
}
