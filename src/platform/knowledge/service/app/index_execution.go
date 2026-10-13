package app
import (
 "context"
 "encoding/json"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/idempotency"
 "knowledge-platform/internal/indexing"
 "github.com/jackc/pgx/v5"
 "time"
)
func(s *IndexService)grant(ctx context.Context,r *indexingv1.ExecutionRequest,v indexing.Version)error{
 for _,id:=range []string{r.GetIndexVersionId(),r.GetOperationId(),r.GetDispatchToken(),r.GetExecutionInstanceId()}{if e:=validID(id);e!=nil{return e}}
 if r.IndexVersionId!=v.ID||r.OperationId!=v.OperationID{return precondition("execution resource mismatch")}
 c,cancel:=context.WithTimeout(ctx,5*time.Second);defer cancel();op,e:=s.chunks.ops.GetOperation(c,&operationv1.GetOperationRequest{OperationId:r.OperationId});if e!=nil{return e}
 if op.GetOperationId()!=v.OperationID||op.GetResourceId()!=v.ID||op.GetResourceType()!="index_version"||op.GetExecutionType()!="indexing.build"||op.GetDispatchProtocol()!=1||op.GetExecutionStartedAt()==nil||op.GetDispatchToken()!=r.DispatchToken||op.GetExecutionInstanceId()!=r.ExecutionInstanceId||(op.GetStatus()!="running"&&!(op.GetStatus()=="succeeded"&&v.State=="ready")){return precondition("execution grant is stale or differs")};return nil
}
func(s *IndexService)execution(ctx context.Context,r *indexingv1.ExecutionRequest)(pgx.Tx,indexing.Version,error){
 var v indexing.Version;if e:=s.enabled();e!=nil{return nil,v,e};if e:=validID(r.GetIndexVersionId());e!=nil{return nil,v,e}
 tx,e:=s.chunks.pool.Begin(ctx);if e!=nil{return nil,v,indexError(e)}
 v,e=scanVersion(tx.QueryRow(ctx,versionSelect+` WHERE index_version_id=$1 FOR UPDATE`,r.IndexVersionId));if e==nil{e=s.grant(ctx,r,v)};if e!=nil{idempotency.Rollback(tx);return nil,v,indexError(e)};return tx,v,nil
}
func(s *IndexService)PrepareBuild(ctx context.Context,r *indexingv1.ExecutionRequest)(*indexingv1.JSONResponse,error){
 tx,v,e:=s.execution(ctx,r);if e!=nil{return nil,e};defer idempotency.Rollback(tx)
 if v.State=="ready"{return jsonIndex(v),nil};if (v.State!="queued"&&v.State!="indexing")||v.ReferenceState!="held"||v.Count<1{return nil,precondition("INDEX_BUILD_NOT_WRITABLE")}
 if e=s.search.Ensure(ctx,v);e!=nil{return nil,unavailable(e.Error())}
 if _,e=tx.Exec(ctx,`UPDATE index_versions SET state='indexing',physical_started=true,updated_at=now() WHERE index_version_id=$1`,v.ID);e!=nil{return nil,indexError(e)};if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};v.State="indexing";return jsonIndex(v),nil
}
func(s *IndexService)ReadBatch(ctx context.Context,r *indexingv1.ReadBatchRequest)(*indexingv1.JSONResponse,error){
 tx,v,e:=s.execution(ctx,r.GetExecution());if e!=nil{return nil,e};defer idempotency.Rollback(tx)
 if v.State!="indexing"||v.Next!=int(r.GetOffset()){return nil,precondition("INDEX_BATCH_OFFSET_CHANGED")}
 chunks,e:=s.loadChunks(ctx,v);if e!=nil{return nil,e};if len(chunks)!=v.Count{return nil,corrupt("pinned chunk count changed")};part,e:=indexing.Slice(chunks,int(r.Offset),v.Profile);if e!=nil{return nil,precondition(e.Error())};return jsonIndex(indexing.Batch{Version:v,Offset:int(r.Offset),Chunks:part}),nil
}
func(s *IndexService)CommitBatch(ctx context.Context,r *indexingv1.CommitBatchRequest)(*indexingv1.JSONResponse,error){
 tx,v,e:=s.execution(ctx,r.GetExecution());if e!=nil{return nil,e};defer idempotency.Rollback(tx)
 if v.State!="indexing"&&v.State!="ready"{return nil,precondition("INDEX_BUILD_NOT_WRITABLE")}
 hash:=indexing.Receipt(r.GetResult());var old string;var committed bool
 e=tx.QueryRow(ctx,`SELECT receipt_hash,committed FROM index_batches WHERE index_version_id=$1 AND offset_value=$2`,v.ID,r.Offset).Scan(&old,&committed)
 if e==nil {if old!=hash{return nil,precondition("INDEX_BATCH_CONFLICT: same offset has different vectors")};if committed{return jsonIndex(v),nil}}else if e!=pgx.ErrNoRows{return nil,indexError(e)}
 if v.State!="indexing"{return nil,precondition("INDEX_BUILD_NOT_WRITABLE")}
 chunks,e:=s.loadChunks(ctx,v);if e!=nil{return nil,e};if len(chunks)!=v.Count{return nil,corrupt("pinned chunk count changed")};part,e:=indexing.Slice(chunks,int(r.Offset),v.Profile);if e!=nil{return nil,invalid(e.Error())};if e=indexing.ValidateResult(v.Profile,part,r.GetResult());e!=nil{return nil,precondition(e.Error())}
 if v.Next!=int(r.Offset){return nil,precondition("INDEX_BATCH_OFFSET_CHANGED")}
 if _,e=tx.Exec(ctx,`INSERT INTO index_batches(index_version_id,offset_value,receipt_hash,chunk_count) VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING`,v.ID,r.Offset,hash,len(part));e!=nil{return nil,indexError(e)}
 // Persist the exact response digest before OpenSearch. A lost DB/bulk reply
 // can only be replayed with these vectors, never a fresh model inference.
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)}
 tx,v,e=s.execution(ctx,r.GetExecution());if e!=nil{return nil,e};defer idempotency.Rollback(tx)
 if v.State!="indexing"{return nil,precondition("INDEX_BUILD_NOT_WRITABLE")}
 if e=tx.QueryRow(ctx,`SELECT receipt_hash,committed FROM index_batches WHERE index_version_id=$1 AND offset_value=$2`,v.ID,r.Offset).Scan(&old,&committed);e!=nil{return nil,indexError(e)}
 if old!=hash{return nil,precondition("INDEX_BATCH_CONFLICT")};if committed{return jsonIndex(v),nil};if v.Next!=int(r.Offset){return nil,precondition("INDEX_BATCH_OFFSET_CHANGED")}
 if e=s.search.Bulk(ctx,v,indexing.Records(v,part,r.Result));e!=nil{return nil,unavailable(e.Error())}
 if _,e=tx.Exec(ctx,`UPDATE index_batches SET committed=true WHERE index_version_id=$1 AND offset_value=$2`,v.ID,r.Offset);e!=nil{return nil,indexError(e)}
 v.Next+=len(part);if _,e=tx.Exec(ctx,`UPDATE index_versions SET indexed_count=$2,updated_at=now() WHERE index_version_id=$1`,v.ID,v.Next);e!=nil{return nil,indexError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};return jsonIndex(v),nil
}
func(s *IndexService)FinishBuild(ctx context.Context,r *indexingv1.ExecutionRequest)(*indexingv1.JSONResponse,error){
 tx,v,e:=s.execution(ctx,r);if e!=nil{return nil,e};defer idempotency.Rollback(tx)
 if v.State=="ready"{return jsonIndex(v),nil};if v.State!="indexing"||v.Count<1||v.Next!=v.Count||v.ReferenceState!="held"{return nil,precondition("INDEX_BUILD_INCOMPLETE")}
 var count int;var pending int
 if e=tx.QueryRow(ctx,`SELECT COALESCE(sum(chunk_count) FILTER(WHERE committed),0),count(*) FILTER(WHERE NOT committed) FROM index_batches WHERE index_version_id=$1`,v.ID).Scan(&count,&pending);e!=nil{return nil,indexError(e)};if count!=v.Count||pending!=0{return nil,precondition("INDEX_BUILD_INCOMPLETE")}
 if e=s.search.Seal(ctx,v);e!=nil{return nil,unavailable(e.Error())}
 if _,e=tx.Exec(ctx,`UPDATE index_versions SET state='ready',error_code='',error_message='',updated_at=now() WHERE index_version_id=$1`,v.ID);e!=nil{return nil,indexError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};v.State="ready";return jsonIndex(v),nil
}
func versionFrom(raw []byte)(indexing.Version,error){var v indexing.Version;e:=json.Unmarshal(raw,&v);return v,e}
