package app
import(
 "context";"encoding/json";"io";"net/http";"strconv";"time"
 chunkingv1 "knowledge-platform/gen/chunking/v1"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 collectionv1 "knowledge-platform/gen/data/collection/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/chunking"
 "google.golang.org/grpc/codes";"google.golang.org/grpc/status"
 "google.golang.org/protobuf/encoding/protojson";"google.golang.org/protobuf/proto"
)
type Server struct{Indexes indexingv1.IndexingServiceClient;Knowledge chunkingv1.ChunkingServiceClient;Operations operationv1.OperationServiceClient;Collection collectionv1.CollectionServiceClient;Refinement refinementv1.RefinementServiceClient;Console http.Handler;ModelConsole *ModelConsole}
func write(w http.ResponseWriter,code int,v any){raw,e:=json.Marshal(v);if e!=nil{http.Error(w,"response encoding failed",500);return};writeRaw(w,code,raw)}
func writeRaw(w http.ResponseWriter,code int,raw []byte){w.Header().Set("Content-Type","application/json; charset=utf-8");w.Header().Set("Cache-Control","no-store");w.Header().Set("X-Content-Type-Options","nosniff");w.WriteHeader(code);_,_=w.Write(raw)}
func writeProto(w http.ResponseWriter,v proto.Message){b,e:=(protojson.MarshalOptions{UseProtoNames:true}).Marshal(v);if e!=nil{http.Error(w,"response encoding failed",500);return};writeRaw(w,200,b)}
func fail(w http.ResponseWriter,e error){code:=status.Code(e);httpCode:=500;switch code{case codes.InvalidArgument:httpCode=400;case codes.NotFound:httpCode=404;case codes.AlreadyExists:httpCode=409;case codes.FailedPrecondition:httpCode=409;case codes.ResourceExhausted:httpCode=413;case codes.Unavailable:httpCode=503;case codes.DeadlineExceeded:httpCode=504;case codes.Canceled:httpCode=408;case codes.DataLoss:httpCode=502}
 message:=status.Convert(e).Message();if httpCode==500{message="request failed; check service state"};write(w,httpCode,map[string]any{"error":map[string]string{"code":code.String(),"message":message}})
}
func limit(r *http.Request)(int32,error){s:=r.URL.Query().Get("limit");if s==""{return 20,nil};n,e:=strconv.Atoi(s);if e!=nil||n<1||n>100{return 0,status.Error(codes.InvalidArgument,"limit must be 1..100")};return int32(n),nil}
func rawOrNil(b []byte)json.RawMessage{if len(b)==0{return nil};return json.RawMessage(b)}
func runView(r *chunkingv1.ChunkingRun)map[string]any{return map[string]any{"chunking_run_id":r.ChunkingRunId,"operation_id":r.OperationId,"name":r.Name,"spec":rawOrNil(r.SpecJson),"result_registered":r.ResultSha256!="","result_sha256":r.ResultSha256,"result_size_bytes":r.ResultSizeBytes,"chunk_count":r.ChunkCount,"summary":rawOrNil(r.SummaryJson),"processor_image_id":r.ProcessorImageId,"created_at":r.GetCreatedAt().AsTime(),"submission_status":r.SubmissionStatus,"submission_error_code":r.SubmissionErrorCode}}
func(s *Server)Handler()http.Handler{
 mux:=http.NewServeMux()
 s.indexRoutes(mux)
 s.consoleModelRoutes(mux)
 mux.HandleFunc("GET /health",func(w http.ResponseWriter,r *http.Request){write(w,200,map[string]string{"status":"ok","scope":"gateway"})})
 mux.HandleFunc("GET /api/chunkers",func(w http.ResponseWriter,r *http.Request){out,e:=s.Knowledge.ListChunkers(r.Context(),&chunkingv1.Empty{});if e!=nil{fail(w,e);return};writeRaw(w,200,out.Json)})
 mux.HandleFunc("POST /api/chunking-runs",s.create)
 mux.HandleFunc("GET /api/chunking-runs",func(w http.ResponseWriter,r *http.Request){n,e:=limit(r);if e!=nil{fail(w,e);return};out,e:=s.Knowledge.ListRuns(r.Context(),&chunkingv1.ListRunsRequest{Limit:n,Cursor:r.URL.Query().Get("cursor")});if e!=nil{fail(w,e);return};items:=[]any{};for _,item:=range out.Items{items=append(items,runView(item))};write(w,200,map[string]any{"items":items,"next_cursor":out.NextCursor})})
 mux.HandleFunc("GET /api/chunking-runs/{id}",func(w http.ResponseWriter,r *http.Request){out,e:=s.Knowledge.GetRun(r.Context(),&chunkingv1.GetRunRequest{ChunkingRunId:r.PathValue("id")});if e!=nil{fail(w,e);return};write(w,200,runView(out))})
 mux.HandleFunc("GET /api/chunking-runs/{id}/chunks",func(w http.ResponseWriter,r *http.Request){n,e:=limit(r);if e!=nil{fail(w,e);return};out,e:=s.Knowledge.ListChunks(r.Context(),&chunkingv1.ListChunksRequest{ChunkingRunId:r.PathValue("id"),Limit:n,Cursor:r.URL.Query().Get("cursor")});if e!=nil{fail(w,e);return};writeRaw(w,200,out.Json)})
 mux.HandleFunc("GET /api/operations/{id}",func(w http.ResponseWriter,r *http.Request){out,e:=s.Operations.GetOperation(r.Context(),&operationv1.GetOperationRequest{OperationId:r.PathValue("id")});if e!=nil{fail(w,e);return};write(w,200,map[string]any{"operation_id":out.OperationId,"status":out.Status,"phase":out.Phase,"error_code":out.ErrorCode,"error_message":out.ErrorMessage,"reconciliation_note":out.ReconcileNote})})
 mux.HandleFunc("GET /api/sources",func(w http.ResponseWriter,r *http.Request){n,e:=limit(r);if e!=nil{fail(w,e);return};out,e:=s.Collection.ListDocuments(r.Context(),&collectionv1.ListDocumentsRequest{Limit:n,Cursor:r.URL.Query().Get("cursor"),Q:r.URL.Query().Get("q"),GroupId:r.URL.Query().Get("group_id")});if e!=nil{fail(w,e);return};writeProto(w,out)})
 mux.HandleFunc("GET /api/source-groups",func(w http.ResponseWriter,r *http.Request){n,e:=limit(r);if e!=nil{fail(w,e);return};out,e:=s.Collection.ListGroups(r.Context(),&collectionv1.ListGroupsRequest{Limit:n,Cursor:r.URL.Query().Get("cursor"),Q:r.URL.Query().Get("q")});if e!=nil{fail(w,e);return};writeProto(w,out)})
 mux.HandleFunc("GET /api/sources/{id}/canonical-versions",func(w http.ResponseWriter,r *http.Request){n,e:=limit(r);if e!=nil{fail(w,e);return};out,e:=s.Refinement.ListRuns(r.Context(),&refinementv1.ListRunsRequest{Limit:n,Cursor:r.URL.Query().Get("cursor"),SourceDocumentId:r.PathValue("id")});if e!=nil{fail(w,e);return};for _,run:=range out.Items{if run.GetSourceDocumentId()!=r.PathValue("id"){fail(w,status.Error(codes.FailedPrecondition,"Data Refinement source filter is unavailable; rebuild the updated Data service"));return}};writeProto(w,out)})
 if s.Console!=nil{mux.Handle("GET /",s.Console)}
 return http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){ctx,cancel:=context.WithTimeout(r.Context(),100*time.Second);defer cancel();mux.ServeHTTP(w,r.WithContext(ctx))})
}
type createBody struct{Name string `json:"name"`;CanonicalIDs []string `json:"canonical_ids"`;Chunker struct{ID string `json:"id"`;Options json.RawMessage `json:"options"`} `json:"chunker"`}
func decodeCreate(raw []byte,key string)(*chunkingv1.CreateRunRequest,error){var b createBody;if chunking.Decode(raw,&b)!=nil{return nil,status.Error(codes.InvalidArgument,"invalid or unknown request fields")};return &chunkingv1.CreateRunRequest{Name:b.Name,CanonicalIds:b.CanonicalIDs,ChunkerId:b.Chunker.ID,OptionsJson:b.Chunker.Options,IdempotencyKey:key},nil}
func(s *Server)create(w http.ResponseWriter,r *http.Request){
 raw,e:=io.ReadAll(http.MaxBytesReader(w,r.Body,64<<10));if e!=nil{fail(w,status.Error(codes.ResourceExhausted,"request exceeds 64 KiB"));return}
 req,e:=decodeCreate(raw,r.Header.Get("Idempotency-Key"));if e!=nil{fail(w,e);return};out,e:=s.Knowledge.CreateRun(r.Context(),req);if e!=nil{fail(w,e);return};write(w,202,map[string]string{"chunking_run_id":out.ChunkingRunId,"operation_id":out.OperationId,"status":"accepted"})
}
