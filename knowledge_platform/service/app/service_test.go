package app
import(
 "context";"encoding/json";"strings";"testing";"time"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/chunking";"knowledge-platform/internal/testsupport"
 "github.com/google/uuid"
 "google.golang.org/grpc/codes";"google.golang.org/grpc/status"
 "google.golang.org/protobuf/types/known/timestamppb"
)
func request()*chunkingv1.CreateRunRequest{return &chunkingv1.CreateRunRequest{Name:"preview",CanonicalIds:[]string{"11111111-1111-4111-8111-111111111111"},ChunkerId:"structure_v1",OptionsJson:[]byte(`{"max_characters":1000,"overlap_characters":100}`),IdempotencyKey:"preview-1"}}
func TestAuthorizationChecksEveryExecutionIdentity(t *testing.T){
 r:=&chunkingv1.CompleteRunRequest{ChunkingRunId:uuid.NewString(),OperationId:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionInstanceId:uuid.NewString()}
 valid:=func()*operationv1.Operation{return &operationv1.Operation{OperationId:r.OperationId,ResourceId:r.ChunkingRunId,ResourceType:"chunking_run",ExecutionType:"chunking.run",DispatchProtocol:1,DispatchToken:r.DispatchToken,ExecutionInstanceId:r.ExecutionInstanceId,ExecutionStartedAt:timestamppb.Now(),Status:"running"}}
 if !authorized(valid(),r){t.Fatal("valid process rejected")}
 for _,mutate:=range []func(*operationv1.Operation){func(o *operationv1.Operation){o.DispatchToken=uuid.NewString()},func(o *operationv1.Operation){o.ExecutionInstanceId=uuid.NewString()},func(o *operationv1.Operation){o.ExecutionStartedAt=nil},func(o *operationv1.Operation){o.ResourceId=uuid.NewString()},func(o *operationv1.Operation){o.Status="failed"},func(o *operationv1.Operation){o.ExecutionType="refinement.parse"}}{o:=valid();mutate(o);if authorized(o,r){t.Fatal("stale or wrong execution accepted")}}
}
func TestCursorsAreBoundToRunAndResult(t *testing.T){
 token:=encodeCursor(chunkCursor{1,"run","hash",2});if offset,e:=decodeCursor(token,"run","hash",4);e!=nil||offset!=2{t.Fatal(e)}
 for _,v:=range [][2]string{{"other","hash"},{"run","other"}}{if _,e:=decodeCursor(token,v[0],v[1],4);status.Code(e)!=codes.InvalidArgument{t.Fatal("unrelated cursor accepted")}}
 if _,e:=decodeCursor(token,"run","hash",2);e==nil{t.Fatal("out of range offset accepted")}
}
type inputFake struct{calls int;offline bool}
func(f *inputFake)Resolve(_ context.Context,id string)(chunking.Input,error){f.calls++;if f.offline{return chunking.Input{},status.Error(codes.Unavailable,"Data offline")};return chunking.Input{CanonicalID:id,CanonicalSHA256:strings.Repeat("a",64),SizeBytes:100,SourceDocumentID:uuid.NewString(),SourceSHA256:strings.Repeat("b",64),SchemaVersion:"canonical-document-v1"},nil}
func TestCreateReplaySurvivesDataOutageAndRejectsChangedBody(t *testing.T){
 pool:=testsupport.Postgres(t);ctx,cancel:=context.WithTimeout(context.Background(),20*time.Second);defer cancel()
 if e:=InitSchema(ctx,pool);e!=nil{t.Fatal(e)};input:=&inputFake{};s:=New(pool,nil,input,nil)
 r:=request();a,e:=s.CreateRun(ctx,r);if e!=nil{t.Fatal(e)};input.offline=true;b,e:=s.CreateRun(ctx,r);if e!=nil||a.ChunkingRunId!=b.GetChunkingRunId()||input.calls!=1{t.Fatal("replay reread Data or changed identity",e)}
 var count int;if e=pool.QueryRow(ctx,`SELECT count(*) FROM operation_submissions`).Scan(&count);e!=nil||count!=1{t.Fatal("submission not atomic",count,e)}
 run,e:=s.GetRun(ctx,&chunkingv1.GetRunRequest{ChunkingRunId:a.ChunkingRunId});if e!=nil{t.Fatal(e)};var spec chunking.Spec;if json.Unmarshal(run.SpecJson,&spec)!=nil||spec.Chunker.ID!="structure_v1"{t.Fatal("spec missing")}
 r.Name="changed";if _,e=s.CreateRun(ctx,r);status.Code(e)!=codes.AlreadyExists{t.Fatal("changed request reused key",e)}
}
