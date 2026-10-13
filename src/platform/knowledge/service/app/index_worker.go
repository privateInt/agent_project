package app
import (
 "context"
 "encoding/json"
 "log"
 "sort"
 "strings"
 "time"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/chunking"
 "knowledge-platform/internal/idempotency"
 "knowledge-platform/internal/indexing"
 "knowledge-platform/internal/submission"
 "github.com/jackc/pgx/v5"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
func referenceRequest(v indexing.Version)*refinementv1.KnowledgeReferencesRequest{
 selected:=map[string]bool{};for _,id:=range v.CanonicalIDs{selected[id]=true};out:=&refinementv1.KnowledgeReferencesRequest{IndexVersionId:v.ID};for _,in:=range v.Spec.Documents{if selected[in.CanonicalID]{out.Documents=append(out.Documents,&refinementv1.CanonicalPin{CanonicalId:in.CanonicalID,Sha256:in.CanonicalSHA256})}};sort.Slice(out.Documents,func(i,j int)bool{return out.Documents[i].CanonicalId<out.Documents[j].CanonicalId});return out
}
func referenceReceipt(req *refinementv1.KnowledgeReferencesRequest,out *refinementv1.KnowledgeReferences)bool{
 if out==nil||out.IndexVersionId!=req.IndexVersionId||out.State!="held"||out.Fingerprint!=chunking.HashJSON(req.Documents)||len(out.Documents)!=len(req.Documents){return false};for i,p:=range req.Documents{if out.Documents[i].GetCanonicalId()!=p.CanonicalId||out.Documents[i].GetSha256()!=p.Sha256{return false}};return true
}
func(s *IndexService)RunIndexMaintenance(ctx context.Context){
 ticker:=time.NewTicker(10*time.Second);defer ticker.Stop()
 for {if s.search!=nil{s.IndexMaintenance(ctx)};select{case<-ctx.Done():return;case<-ticker.C:}}
}
func(s *IndexService)IndexMaintenance(ctx context.Context){
 q,cancel:=context.WithTimeout(ctx,5*time.Second);rows,e:=s.chunks.pool.Query(q,`SELECT index_version_id::text FROM index_versions WHERE state IN ('protecting','queued','indexing','deleting') ORDER BY updated_at,index_version_id LIMIT 20`);if e!=nil{cancel();return};ids:=[]string{};for rows.Next(){var id string;if rows.Scan(&id)==nil{ids=append(ids,id)}};rows.Close();cancel()
 for _,id:=range ids{c,stop:=context.WithTimeout(ctx,90*time.Second);e=s.maintainVersion(c,id);stop();if e!=nil{log.Printf("index maintenance version=%s unconfirmed",id)}}
 // Parent completion is a separate transaction; never acquire it under a version lock.
 q,cancel=context.WithTimeout(ctx,5*time.Second);defer cancel()
 _,_=s.chunks.pool.Exec(q,`UPDATE logical_indexes i SET state='deleted' WHERE state='deleting' AND NOT EXISTS(SELECT 1 FROM index_versions v WHERE v.index_id=i.index_id AND v.state!='deleted')`)
}
func(s *IndexService)maintainVersion(ctx context.Context,id string)error{
 tx,e:=s.chunks.pool.Begin(ctx);if e!=nil{return e};defer idempotency.Rollback(tx)
 v,e:=scanVersion(tx.QueryRow(ctx,versionSelect+` WHERE index_version_id=$1 FOR UPDATE SKIP LOCKED`,id));if e==pgx.ErrNoRows{return nil};if e!=nil{return e}
 save:=func()error{raw,_:=json.Marshal(v);_,e:=tx.Exec(ctx,`UPDATE index_versions SET spec_json=$2,state=$3,chunk_count=$4,indexed_count=$5,reference_state=$6,error_code=$7,error_message=$8,updated_at=now() WHERE index_version_id=$1`,v.ID,raw,v.State,v.Count,v.Next,v.ReferenceState,v.ErrorCode,v.ErrorMessage);if e!=nil{return e};return tx.Commit(ctx)}
 later:=func(code,msg string)error{v.ErrorCode=code;v.ErrorMessage=msg;return save()}
 if v.State=="deleting"{
  if e=s.search.Delete(ctx,v);e!=nil{return later("INDEX_DELETE_PENDING",e.Error())}
  out,e:=s.references.ReleaseKnowledgeReferences(ctx,&refinementv1.KnowledgeReferenceOwner{IndexVersionId:v.ID});if e!=nil||out.GetIndexVersionId()!=v.ID||out.GetState()!="released"{return later("REFERENCE_RELEASE_PENDING","Index removal confirmed; Data reference release will be retried")}
  v.State="deleted";v.ReferenceState="released";v.ErrorCode="";v.ErrorMessage="";return save()
 }
 if v.State=="queued"||v.State=="indexing"{
  op,e:=s.chunks.ops.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:v.OperationID});if e!=nil{return later("OPERATION_OBSERVATION_PENDING","Build status temporarily unavailable")}
  if op.GetResourceId()!=v.ID||op.GetResourceType()!="index_version"||op.GetExecutionType()!="indexing.build"{return later("OPERATION_IDENTITY_CONFLICT","Operation identity differs")}
  if op.Status=="failed"||op.Status=="cancelled"{v.State="failed";v.ErrorCode=op.ErrorCode;v.ErrorMessage=op.ErrorMessage;if v.ErrorCode==""{v.ErrorCode="INDEX_BUILD_STOPPED"}}else if strings.Contains(op.GetReconcileNote(),"exited index process"){v.ErrorCode="INDEX_EXECUTION_UNCONFIRMED";v.ErrorMessage="Process exited without a final receipt. Inspect execution status; delete this version or build a new version."}else{v.ErrorCode="";v.ErrorMessage=""}
  return save()
 }
 if v.State!="protecting"{return nil}
 if v.ReferenceState!="held"{
  req:=referenceRequest(v);out,e:=s.references.AcquireKnowledgeReferences(ctx,req)
  if e!=nil {if status.Code(e)==codes.FailedPrecondition||status.Code(e)==codes.NotFound{v.State="failed"};return later("REFERENCE_ACQUIRE_PENDING","Pinned Data documents could not be protected; inspect availability or delete this version")}
  if !referenceReceipt(req,out){v.State="failed";return later("REFERENCE_RECEIPT_MISMATCH","Data reference receipt differs")};v.ReferenceState="held"
 }
 run,e:=s.chunks.GetRun(ctx,&chunkingv1.GetRunRequest{ChunkingRunId:v.ChunkingRunID});if e!=nil{return later("CHUNK_OBSERVATION_PENDING","Pinned chunking run temporarily unavailable")}
 if run.ResultSha256==""{
  op,e:=s.chunks.ops.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:run.OperationId});if e==nil&&(op.Status=="failed"||op.Status=="cancelled"){v.State="failed";return later("CHUNKING_FAILED",op.ErrorMessage)}
  v.ErrorCode="";v.ErrorMessage="Waiting for chunking completion";return save()
 }
 if v.ChunkSHA!=""&&v.ChunkSHA!=run.ResultSha256{v.State="failed";return later("CHUNK_SET_CHANGED","Immutable ChunkSet identity differs")};v.ChunkSHA=run.ResultSha256
 chunks,e:=s.loadChunks(ctx,v);if e!=nil{if status.Code(e)==codes.DataLoss{v.State="failed"};return later("CHUNK_VERIFICATION_PENDING","Chunk artifact verification failed")};if len(chunks)<1{v.State="failed";return later("EMPTY_INDEX_INPUT","Selected documents contain no chunks")};v.Count=len(chunks)
 if e=submission.Enqueue(ctx,tx,&operationv1.CreateOperationRequest{OperationId:v.OperationID,Kind:"indexing.build",ExecutionType:"indexing.build",ResourceType:"index_version",ResourceId:v.ID,MaxAttempts:1});e!=nil{return e}
 v.State="queued";v.ErrorCode="";v.ErrorMessage="";return save()
}
