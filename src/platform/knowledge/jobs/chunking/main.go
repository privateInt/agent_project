package main
import(
 "bytes";"context";"encoding/json";"fmt";"log";"time"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 collectionv1 "knowledge-platform/gen/data/collection/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/blob";"knowledge-platform/internal/chunking";"knowledge-platform/internal/config";"knowledge-platform/internal/operationreport"
 dataclient "knowledge-platform/internal/clients/data"
 "google.golang.org/grpc";"google.golang.org/grpc/credentials/insecure"
)
func main(){if e:=run();e!=nil{log.Fatal(e)}}
func run()error{
 ctx,cancel:=context.WithTimeout(context.Background(),10*time.Minute);defer cancel()
 opID,runID:=config.Required("OPERATION_ID"),config.Required("RESOURCE_ID")
 connect:=func(key string)(*grpc.ClientConn,error){return grpc.NewClient(config.Required(key),grpc.WithTransportCredentials(insecure.NewCredentials()))}
 op,e:=connect("OPERATION_GRPC_ADDRESS");if e!=nil{return e};defer op.Close()
 reporter:=operationreport.New(operationv1.NewOperationServiceClient(op))
 // No Data reads, blob writes or chunking before this confirmed grant.
 if e=reporter.Authorize(ctx,opID,runID,"chunking.run");e!=nil{return fmt.Errorf("execution authorization denied or unconfirmed")}
 fail:=func(code,message string)error{c,stop:=context.WithTimeout(context.Background(),20*time.Second);defer stop();_,e:=reporter.FailOperation(c,&operationv1.FailOperationRequest{OperationId:opID,Phase:"failed",ErrorCode:code,ErrorMessage:message,Retryable:false});if e!=nil{return fmt.Errorf("%s: failure report unconfirmed",code)};return fmt.Errorf("%s: %s",code,message)}
 phase:=func(name string){c,stop:=context.WithTimeout(ctx,3*time.Second);defer stop();_,_=reporter.UpdatePhase(c,&operationv1.UpdateOperationRequest{OperationId:opID,Phase:name})}
 knowledge,e:=connect("KNOWLEDGE_GRPC_ADDRESS");if e!=nil{return fail("CHUNK_INPUT_UNAVAILABLE","Knowledge service unavailable")};defer knowledge.Close()
 client:=chunkingv1.NewChunkingServiceClient(knowledge)
 run,e:=client.GetRun(ctx,&chunkingv1.GetRunRequest{ChunkingRunId:runID});if e!=nil||run.GetOperationId()!=opID{return fail("CHUNK_INPUT_UNAVAILABLE","Chunking request identity unavailable")}
 var spec chunking.Spec;if chunking.Decode(run.SpecJson,&spec)!=nil{return fail("CHUNK_SPEC_INVALID","Stored specification is invalid")}
 col,e:=connect("DATA_COLLECTION_GRPC_ADDRESS");if e!=nil{return fail("CHUNK_INPUT_UNAVAILABLE","Data Collection unavailable")};defer col.Close()
 ref,e:=connect("DATA_REFINEMENT_GRPC_ADDRESS");if e!=nil{return fail("CHUNK_INPUT_UNAVAILABLE","Data Refinement unavailable")};defer ref.Close()
 data:=dataclient.Client{Collection:collectionv1.NewCollectionServiceClient(col),Refinement:refinementv1.NewRefinementServiceClient(ref)}
 phase("reading_canonical")
 raws:=map[string][]byte{}
 for _,in:=range spec.Documents {raw,e:=data.Read(ctx,in);if e!=nil{return fail("CHUNK_INPUT_UNAVAILABLE","Pinned canonical is missing, changed or unreadable: "+in.CanonicalID)};raws[in.CanonicalID]=raw}
 phase("chunking")
 started:=time.Now();result,e:=chunking.Build(spec,raws);raws=nil
 if e!=nil{return fail("CHUNKING_FAILED",e.Error())}
 raw,e:=json.Marshal(result);if e!=nil{return fail("CHUNKING_FAILED","Cannot encode chunk result")}
 summary,_:=json.Marshal(map[string]any{"documents":result.Documents,"exclusions":result.Exclusions,"chunk_count":len(result.Chunks)})
 if len(summary)>1<<20{return fail("CHUNK_PREVIEW_LIMIT","Preview summary exceeds 1 MiB")}
 if _,e=chunking.Verify(raw,spec);e!=nil{return fail("CHUNKING_FAILED","Generated result validation failed")}
 log.Printf("knowledge_job_timing_v1 operation_id=%s stage=chunking elapsed_ms=%d chunks=%d",opID,time.Since(started).Milliseconds(),len(result.Chunks))
 phase("storing_chunks")
 store,e:=blob.Connect(ctx,config.Required("MINIO_ENDPOINT"),config.Required("MINIO_ACCESS_KEY"),config.Required("MINIO_SECRET_KEY"),config.Bool("MINIO_USE_SSL",false));if e!=nil{return fail("CHUNK_STORAGE_FAILED","Knowledge chunk storage unavailable")}
 digest:=chunking.Digest(raw);key,e:=blob.Key(runID,digest);if e!=nil{return fail("CHUNK_STORAGE_FAILED","Invalid result key")}
 if e=store.Put(ctx,key,bytes.NewReader(raw),int64(len(raw)),"application/json");e!=nil{return fail("CHUNK_STORAGE_FAILED","Result upload unconfirmed; no registration attempted")}
 token,instance:=reporter.ExecutionIdentity()
 req:=&chunkingv1.CompleteRunRequest{ChunkingRunId:runID,OperationId:opID,DispatchToken:token,ExecutionInstanceId:instance,Sha256:digest,SizeBytes:int64(len(raw)),ProcessorImageId:config.Required("PROCESSOR_IMAGE_ID")}
 var done *chunkingv1.ChunkingRun
 for attempt:=0;attempt<3;attempt++{c,stop:=context.WithTimeout(ctx,60*time.Second);done,e=client.CompleteRun(c,req);stop();if e==nil||ctx.Err()!=nil{break}}
 // Never report failure after an ambiguous commit. Reconciler checks the
 // already registered immutable artifact independently of this process.
 if e!=nil||done.GetResultSha256()!=digest{return fmt.Errorf("chunk result registration unconfirmed; reconciliation required")}
 c,stop:=context.WithTimeout(context.Background(),20*time.Second);defer stop()
 if _,e=reporter.SucceedOperation(c,&operationv1.UpdateOperationRequest{OperationId:opID,Phase:"completed"});e!=nil{return fmt.Errorf("chunks registered; completion report requires reconciliation")}
 return nil
}
