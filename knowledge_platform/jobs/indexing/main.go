package main
import (
 "context"
 "encoding/json"
 "fmt"
 "log"
 "time"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 embeddingv1 "knowledge-platform/gen/model/embedding/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/config"
 "knowledge-platform/internal/indexing"
 "knowledge-platform/internal/operationreport"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/credentials/insecure"
 "google.golang.org/grpc/status"
)
func main(){if e:=run();e!=nil{log.Fatal(e)}}
func run()error{
 ctx,cancel:=context.WithTimeout(context.Background(),2*time.Hour);defer cancel()
 connect:=func(key string)(*grpc.ClientConn,error){return grpc.NewClient(config.Required(key),grpc.WithTransportCredentials(insecure.NewCredentials()))}
 op,e:=connect("OPERATION_GRPC_ADDRESS");if e!=nil{return e};defer op.Close();report:=operationreport.New(operationv1.NewOperationServiceClient(op))
 id,version:=config.Required("OPERATION_ID"),config.Required("RESOURCE_ID")
 if e=report.Authorize(ctx,id,version,"indexing.build");e!=nil{return fmt.Errorf("index execution grant unconfirmed")}
 fail:=func(code,msg string)error{c,stop:=context.WithTimeout(context.Background(),20*time.Second);defer stop();_,_=report.FailOperation(c,&operationv1.FailOperationRequest{OperationId:id,Phase:"failed",ErrorCode:code,ErrorMessage:msg,Retryable:false});return fmt.Errorf("%s: %s",code,msg)}
 phase:=func(name string){c,stop:=context.WithTimeout(ctx,3*time.Second);defer stop();_,_=report.UpdatePhase(c,&operationv1.UpdateOperationRequest{OperationId:id,Phase:name})}
 k,e:=connect("KNOWLEDGE_GRPC_ADDRESS");if e!=nil{return fail("INDEX_SERVICE_UNAVAILABLE","Knowledge connection unavailable")};defer k.Close();client:=indexingv1.NewIndexingServiceClient(k)
 m,e:=connect("MODEL_EMBEDDING_GRPC_ADDRESS");if e!=nil{return fail("MODEL_UNAVAILABLE","Model connection unavailable")};defer m.Close();model:=embeddingv1.NewEmbeddingServiceClient(m)
 token,instance:=report.ExecutionIdentity();grant:=&indexingv1.ExecutionRequest{IndexVersionId:version,OperationId:id,DispatchToken:token,ExecutionInstanceId:instance}
 // Retry only identical, bounded Knowledge RPCs. Never retry a Model inference.
 call:=func(fn func(context.Context)(*indexingv1.JSONResponse,error))(*indexingv1.JSONResponse,error){var out *indexingv1.JSONResponse;var e error;for i:=0;i<3;i++{c,stop:=context.WithTimeout(ctx,90*time.Second);out,e=fn(c);stop();if e==nil||ctx.Err()!=nil||status.Code(e)!=codes.Unavailable&&status.Code(e)!=codes.DeadlineExceeded&&status.Code(e)!=codes.Internal{break}};return out,e}
 raw,e:=call(func(c context.Context)(*indexingv1.JSONResponse,error){return client.PrepareBuild(c,grant)});if e!=nil{return fail("INDEX_PREPARE_FAILED",status.Convert(e).Message())}
 var v indexing.Version;if json.Unmarshal(raw.Json,&v)!=nil||v.ID!=version||v.OperationID!=id{return fail("INDEX_RECEIPT_INVALID","Build identity differs")}
 for v.State!="ready"&&v.Next<v.Count{
  phase(fmt.Sprintf("embedding_%d_of_%d",v.Next,v.Count))
  raw,e=call(func(c context.Context)(*indexingv1.JSONResponse,error){return client.ReadBatch(c,&indexingv1.ReadBatchRequest{Execution:grant,Offset:int32(v.Next)})});if e!=nil{return fail("INDEX_INPUT_FAILED",status.Convert(e).Message())}
  var batch indexing.Batch;if json.Unmarshal(raw.Json,&batch)!=nil||batch.Version.ID!=v.ID||batch.Offset!=v.Next||len(batch.Chunks)==0{return fail("INDEX_INPUT_INVALID","Chunk batch differs")}
  inputs:=[]*embeddingv1.EmbeddingInput{};for _,chunk:=range batch.Chunks{inputs=append(inputs,&embeddingv1.EmbeddingInput{Id:chunk.ID,Text:chunk.Text})}
  c,stop:=context.WithTimeout(ctx,125*time.Second);result,e:=model.Embed(c,&embeddingv1.EmbedRequest{DeploymentId:v.Profile.DeploymentId,ExpectedEmbeddingSpaceId:v.Profile.EmbeddingSpaceId,Role:"document",Inputs:inputs});stop()
  if e!=nil{return fail("MODEL_EMBEDDING_FAILED",status.Convert(e).Message())};if e=indexing.ValidateResult(v.Profile,batch.Chunks,result);e!=nil{return fail("MODEL_RECEIPT_INVALID",e.Error())}
  request:=&indexingv1.CommitBatchRequest{Execution:grant,Offset:int32(v.Next),Result:result}
  raw,e=call(func(c context.Context)(*indexingv1.JSONResponse,error){return client.CommitBatch(c,request)});if e!=nil{return fail("INDEX_BATCH_UNCONFIRMED",status.Convert(e).Message())}
  var next indexing.Version;if json.Unmarshal(raw.Json,&next)!=nil||next.ID!=v.ID||next.Next!=v.Next+len(batch.Chunks)||next.Count!=v.Count{return fail("INDEX_RECEIPT_INVALID","Stored batch count differs")};v=next
 }
 phase("verifying_index")
 raw,e=call(func(c context.Context)(*indexingv1.JSONResponse,error){return client.FinishBuild(c,grant)})
 // A final commit may be durable even if the reply was lost. Reconciliation
 // observes the domain receipt; never overwrite that outcome with failure.
 if e!=nil{return fmt.Errorf("index final receipt unconfirmed; reconciliation required")}
 if json.Unmarshal(raw.Json,&v)!=nil||v.ID!=version||v.State!="ready"{return fmt.Errorf("index final receipt invalid; reconciliation required")}
 c,stop:=context.WithTimeout(context.Background(),20*time.Second);defer stop();if _,e=report.SucceedOperation(c,&operationv1.UpdateOperationRequest{OperationId:id,Phase:"completed"});e!=nil{return fmt.Errorf("index ready; operation report reconciliation required")};return nil
}
