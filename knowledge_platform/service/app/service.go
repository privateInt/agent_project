package app

import (
 "context"
 "encoding/json"
 "errors"
 "fmt"
 "sort"
 "strings"
 "time"
 "unicode/utf8"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/blob"
 "knowledge-platform/internal/chunking"
 "knowledge-platform/internal/idempotency"
 "knowledge-platform/internal/listing"
 "knowledge-platform/internal/submission"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5"
 "github.com/jackc/pgx/v5/pgxpool"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/types/known/timestamppb"
)

type Inputs interface {Resolve(context.Context,string)(chunking.Input,error)}
type Service struct {
 chunkingv1.UnimplementedChunkingServiceServer
 pool *pgxpool.Pool
 ops operationv1.OperationServiceClient
 inputs Inputs
 store blob.Store
 reads chan struct{}
}
func New(pool *pgxpool.Pool,ops operationv1.OperationServiceClient,inputs Inputs,store blob.Store)*Service{return &Service{pool:pool,ops:ops,inputs:inputs,store:store,reads:make(chan struct{},2)}}
func InitSchema(ctx context.Context,pool *pgxpool.Pool)error{_,e:=pool.Exec(ctx,idempotency.Schema+submission.Schema+indexSchema+`
CREATE TABLE IF NOT EXISTS chunking_runs (
 chunking_run_id UUID PRIMARY KEY, operation_id UUID NOT NULL UNIQUE,
 name TEXT NOT NULL, spec_json BYTEA NOT NULL,
 result_sha256 TEXT NOT NULL DEFAULT '', result_size_bytes BIGINT NOT NULL DEFAULT 0,
 chunk_count INTEGER NOT NULL DEFAULT 0, summary_json BYTEA NOT NULL DEFAULT ''::bytea,
 processor_image_id TEXT NOT NULL DEFAULT '', completion_hash TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS chunking_runs_created_idx ON chunking_runs(created_at DESC,chunking_run_id DESC);
`);return e}
func(s *Service)RunSubmissions(ctx context.Context){submission.Worker{Pool:s.pool,Ensure:func(c context.Context,r *operationv1.CreateOperationRequest)(*operationv1.Operation,error){return s.ops.EnsureOperation(c,r)}}.Run(ctx)}
func validID(id string)error{u,e:=uuid.Parse(id);if e!=nil||u==uuid.Nil||u.String()!=id{return status.Error(codes.InvalidArgument,"canonical nonzero UUID is required")};return nil}
func readError(e error)error{if errors.Is(e,pgx.ErrNoRows){return status.Error(codes.NotFound,"chunking run not found")};return idempotency.DBError(e)}
func(s *Service)ListChunkers(context.Context,*chunkingv1.Empty)(*chunkingv1.JSONResponse,error){raw,_:=json.Marshal(map[string]any{"items":chunking.Describe()});return &chunkingv1.JSONResponse{Json:raw},nil}

func normalize(r *chunkingv1.CreateRunRequest)(string,[]string,chunking.Selection,string,error){
 name:=strings.TrimSpace(r.GetName());if name==""||!utf8.ValidString(name)||utf8.RuneCountInString(name)>120||strings.ContainsRune(name,0){return "",nil,chunking.Selection{},"",status.Error(codes.InvalidArgument,"name must contain 1..120 characters")}
 if len(r.GetCanonicalIds())<1||len(r.GetCanonicalIds())>chunking.MaxDocuments{return "",nil,chunking.Selection{},"",status.Error(codes.InvalidArgument,"choose 1..20 canonical documents")}
 ids:=append([]string(nil),r.CanonicalIds...);sort.Strings(ids)
 for i,id:=range ids {if e:=validID(id);e!=nil{return "",nil,chunking.Selection{},"",e};if i>0&&ids[i-1]==id{return "",nil,chunking.Selection{},"",status.Error(codes.InvalidArgument,"duplicate canonical ID")}}
 selected,e:=chunking.Normalize(r.GetChunkerId(),r.GetOptionsJson());if e!=nil{return "",nil,selected,"",status.Error(codes.InvalidArgument,e.Error())}
 if r.GetIdempotencyKey()==""{return "",nil,selected,"",status.Error(codes.InvalidArgument,"Idempotency-Key is required")};if e=idempotency.ValidateKey(r.IdempotencyKey);e!=nil{return "",nil,selected,"",e}
 declared:=map[string]json.RawMessage{}
 if len(r.OptionsJson)>0 {if e=json.Unmarshal(r.OptionsJson,&declared);e!=nil{return "",nil,selected,"",status.Error(codes.InvalidArgument,"invalid options")}}
 fp,e:=idempotency.Fingerprint(struct{Version,Name,ChunkerID string;IDs []string;Options map[string]json.RawMessage}{"chunking-request-v1",name,r.ChunkerId,ids,declared},nil)
 return name,ids,selected,fp,e
}
func(s *Service)CreateRun(ctx context.Context,r *chunkingv1.CreateRunRequest)(*chunkingv1.RunAccepted,error){
 name,ids,selected,fp,e:=normalize(r);if e!=nil{return nil,e}
 out:=&chunkingv1.RunAccepted{ChunkingRunId:uuid.NewString(),OperationId:uuid.NewString()}
 e=idempotency.ExecuteWithTimeout(ctx,s.pool,"chunking.create",r.IdempotencyKey,fp,out,90*time.Second,func(ctx context.Context,tx pgx.Tx)error{
  inputs:=make([]chunking.Input,0,len(ids))
  for _,id:=range ids {in,e:=s.inputs.Resolve(ctx,id);if e!=nil{return e};inputs=append(inputs,in)}
  spec,e:=chunking.NewSpec(inputs,selected);if e!=nil{return status.Error(codes.FailedPrecondition,e.Error())}
  raw,_:=json.Marshal(spec)
  if _,e=tx.Exec(ctx,`INSERT INTO chunking_runs(chunking_run_id,operation_id,name,spec_json) VALUES($1,$2,$3,$4)`,out.ChunkingRunId,out.OperationId,name,raw);e!=nil{return e}
  return submission.Enqueue(ctx,tx,&operationv1.CreateOperationRequest{OperationId:out.OperationId,Kind:"chunking.run",ExecutionType:"chunking.run",ResourceType:"chunking_run",ResourceId:out.ChunkingRunId,MaxAttempts:1})
 });return out,e
}

const runSelect=`SELECT chunking_run_id::text,operation_id::text,name,spec_json,result_sha256,result_size_bytes,chunk_count,summary_json,processor_image_id,created_at FROM chunking_runs`
func scan(row pgx.Row)(*chunkingv1.ChunkingRun,error){r:=&chunkingv1.ChunkingRun{};var t time.Time;e:=row.Scan(&r.ChunkingRunId,&r.OperationId,&r.Name,&r.SpecJson,&r.ResultSha256,&r.ResultSizeBytes,&r.ChunkCount,&r.SummaryJson,&r.ProcessorImageId,&t);r.CreatedAt=timestamppb.New(t);return r,e}
func(s *Service)GetRun(ctx context.Context,r *chunkingv1.GetRunRequest)(*chunkingv1.ChunkingRun,error){
 if e:=validID(r.GetChunkingRunId());e!=nil{return nil,e}
 run,e:=scan(s.pool.QueryRow(ctx,runSelect+` WHERE chunking_run_id=$1`,r.ChunkingRunId));if e!=nil{return nil,readError(e)}
 info,e:=submission.Lookup(ctx,s.pool,"chunking_run",run.ChunkingRunId);if e!=nil{return nil,idempotency.DBError(e)};run.SubmissionStatus=info.Status;run.SubmissionErrorCode=info.ErrorCode;return run,nil
}
func(s *Service)ListRuns(ctx context.Context,r *chunkingv1.ListRunsRequest)(*chunkingv1.ListRunsResponse,error){
 page,e:=listing.New(r.GetLimit(),r.GetCursor(),"chunking-runs");if e!=nil{return nil,e}
 q:=listing.Query{};suffix:=q.Finish(page,"created_at","chunking_run_id")
 lightSelect:=`SELECT chunking_run_id::text,operation_id::text,name,''::bytea,result_sha256,result_size_bytes,chunk_count,''::bytea,processor_image_id,created_at FROM chunking_runs`
 rows,e:=s.pool.Query(ctx,lightSelect+suffix,q.Args...);if e!=nil{return nil,idempotency.DBError(e)};defer rows.Close()
 out:=&chunkingv1.ListRunsResponse{}
 for rows.Next(){if len(out.Items)==page.Limit{last:=out.Items[len(out.Items)-1];out.NextCursor=page.Next(last.CreatedAt.AsTime(),last.ChunkingRunId);break};run,e:=scan(rows);if e!=nil{return nil,idempotency.DBError(e)};out.Items=append(out.Items,run)}
 return out,idempotency.DBError(rows.Err())
}
func authorized(op *operationv1.Operation,r *chunkingv1.CompleteRunRequest)bool{return op!=nil&&op.GetOperationId()==r.GetOperationId()&&op.GetResourceId()==r.GetChunkingRunId()&&op.GetResourceType()=="chunking_run"&&op.GetExecutionType()=="chunking.run"&&op.GetDispatchProtocol()==1&&op.GetExecutionStartedAt()!=nil&&op.GetDispatchToken()==r.GetDispatchToken()&&op.GetExecutionInstanceId()==r.GetExecutionInstanceId()&&(op.GetStatus()=="running"||op.GetStatus()=="succeeded")}
func(s *Service)CompleteRun(ctx context.Context,r *chunkingv1.CompleteRunRequest)(*chunkingv1.ChunkingRun,error){
 for _,id:=range []string{r.GetChunkingRunId(),r.GetOperationId(),r.GetDispatchToken(),r.GetExecutionInstanceId()}{if e:=validID(id);e!=nil{return nil,e}}
 if !chunking.ValidDigest(r.GetSha256())||r.GetSizeBytes()<1||r.GetSizeBytes()>chunking.MaxOutputBytes||!strings.HasPrefix(r.GetProcessorImageId(),"sha256:")||!chunking.ValidDigest(strings.TrimPrefix(r.GetProcessorImageId(),"sha256:")){return nil,status.Error(codes.InvalidArgument,"invalid chunk result identity")}
 check:=func()error{c,cancel:=context.WithTimeout(ctx,5*time.Second);defer cancel();op,e:=s.ops.GetOperation(c,&operationv1.GetOperationRequest{OperationId:r.OperationId});if e!=nil{return e};if !authorized(op,r){return status.Error(codes.FailedPrecondition,"result is not from the authorized chunking process")};return nil}
 if e:=check();e!=nil{return nil,e}
 select{case s.reads<-struct{}{}:defer func(){<-s.reads}();case <-ctx.Done():return nil,status.FromContextError(ctx.Err()).Err()}
 tx,e:=s.pool.Begin(ctx);if e!=nil{return nil,idempotency.DBError(e)};defer idempotency.Rollback(tx)
 run,e:=scan(tx.QueryRow(ctx,runSelect+` WHERE chunking_run_id=$1 FOR UPDATE`,r.ChunkingRunId));if e!=nil{return nil,readError(e)}
 if run.OperationId!=r.OperationId{return nil,status.Error(codes.FailedPrecondition,"operation mismatch")}
 fp:=chunking.HashJSON(struct{SHA,Image string;Size int64}{r.Sha256,r.ProcessorImageId,r.SizeBytes})
 if run.ResultSha256!="" {var prior string;if e=tx.QueryRow(ctx,`SELECT completion_hash FROM chunking_runs WHERE chunking_run_id=$1`,run.ChunkingRunId).Scan(&prior);e!=nil{return nil,idempotency.DBError(e)};if prior!=fp{return nil,status.Error(codes.AlreadyExists,"immutable result differs")};return run,nil}
 key,_:=blob.Key(run.ChunkingRunId,r.Sha256);raw,e:=blob.Read(ctx,s.store,key,r.Sha256,r.SizeBytes);if e!=nil{return nil,status.Error(codes.FailedPrecondition,"stored chunk result integrity verification failed")}
 var spec chunking.Spec;if chunking.Decode(run.SpecJson,&spec)!=nil{return nil,status.Error(codes.DataLoss,"stored spec is invalid")}
 result,e:=chunking.Verify(raw,spec);if e!=nil{return nil,status.Error(codes.FailedPrecondition,e.Error())}
 summary,_:=json.Marshal(map[string]any{"documents":result.Documents,"exclusions":result.Exclusions,"chunk_count":len(result.Chunks)})
 if len(summary)>1<<20{return nil,status.Error(codes.ResourceExhausted,"preview summary exceeds 1 MiB")}
 if e=check();e!=nil{return nil,e}
 _,e=tx.Exec(ctx,`UPDATE chunking_runs SET result_sha256=$2,result_size_bytes=$3,chunk_count=$4,summary_json=$5,processor_image_id=$6,completion_hash=$7 WHERE chunking_run_id=$1`,run.ChunkingRunId,r.Sha256,r.SizeBytes,len(result.Chunks),summary,r.ProcessorImageId,fp);if e!=nil{return nil,idempotency.DBError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,idempotency.DBError(e)}
 run.ResultSha256=r.Sha256;run.ResultSizeBytes=r.SizeBytes;run.ChunkCount=int32(len(result.Chunks));run.SummaryJson=summary;run.ProcessorImageId=r.ProcessorImageId;return run,nil
}

type chunkCursor struct{Version int `json:"version"`;Run string `json:"run"`;Hash string `json:"hash"`;Offset int `json:"offset"`}
func(s *Service)ListChunks(ctx context.Context,r *chunkingv1.ListChunksRequest)(*chunkingv1.JSONResponse,error){
 limit:=int(r.GetLimit());if limit==0{limit=20};if limit<1||limit>100{return nil,status.Error(codes.InvalidArgument,"limit must be 1..100")}
 run,e:=s.GetRun(ctx,&chunkingv1.GetRunRequest{ChunkingRunId:r.GetChunkingRunId()});if e!=nil{return nil,e}
 if run.ResultSha256==""{return nil,status.Error(codes.FailedPrecondition,"chunk result is not registered yet")}
 offset,e:=decodeCursor(r.GetCursor(),run.ChunkingRunId,run.ResultSha256,int(run.ChunkCount));if e!=nil{return nil,e}
 select{case s.reads<-struct{}{}:defer func(){<-s.reads}();case <-ctx.Done():return nil,status.FromContextError(ctx.Err()).Err()}
 key,_:=blob.Key(run.ChunkingRunId,run.ResultSha256);raw,e:=blob.Read(ctx,s.store,key,run.ResultSha256,run.ResultSizeBytes);if e!=nil{return nil,status.Error(codes.DataLoss,"chunk artifact integrity failed")}
 var spec chunking.Spec;if chunking.Decode(run.SpecJson,&spec)!=nil{return nil,status.Error(codes.DataLoss,"stored spec is invalid")};result,e:=chunking.Verify(raw,spec);if e!=nil{return nil,status.Error(codes.DataLoss,"chunk artifact validation failed")}
 end:=min(offset+limit,len(result.Chunks))
 for end>offset {
  next:="";if end<len(result.Chunks){next=encodeCursor(chunkCursor{1,run.ChunkingRunId,run.ResultSha256,end})}
  body,_:=json.Marshal(map[string]any{"chunking_run_id":run.ChunkingRunId,"total":len(result.Chunks),"items":result.Chunks[offset:end],"next_cursor":next})
  if len(body)<=2<<20{return &chunkingv1.JSONResponse{Json:body},nil};end--
 }
 return nil,status.Error(codes.ResourceExhausted,fmt.Sprintf("single chunk exceeds preview response limit at %d",offset))
}
