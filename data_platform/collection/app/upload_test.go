package app

import (
 "bytes"
 "context"
 "crypto/sha256"
 "encoding/hex"
 "errors"
 "io"
 "sync"
 "testing"

 collectionv1 "data-platform/gen/collection/v1"
 "data-platform/internal/blob"
 "data-platform/internal/idempotency"
 "data-platform/internal/sourcefile"
 "data-platform/internal/submission"
 "data-platform/internal/testsupport"
 "github.com/google/uuid"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
)

type uploadStore struct{mu sync.Mutex;objects map[string][]byte;puts int;fail,corrupt bool}
func(m *uploadStore)Put(_ context.Context,key string,r io.Reader,_ int64,_ string)error{
 m.mu.Lock();defer m.mu.Unlock();m.puts++;if m.fail{return errors.New("storage offline")};b,e:=io.ReadAll(r);if e!=nil{return e};if m.corrupt{b=bytes.Repeat([]byte("x"),len(b))};m.objects[key]=b;return nil
}
func(m *uploadStore)Open(_ context.Context,key string)(io.ReadCloser,error){m.mu.Lock();defer m.mu.Unlock();data,ok:=m.objects[key];if !ok{return nil,errors.New("missing")};return io.NopCloser(bytes.NewReader(bytes.Clone(data))),nil}
func(m *uploadStore)URI(key string)string{return "s3://data-sources/"+key}
func uploadFixture(t *testing.T)(*Service,*uploadStore){s,_,_:=fixture(t);m:=&uploadStore{objects:map[string][]byte{}};s.store=m;return s,m}
func uploadMetadata(key string,data []byte)*collectionv1.UploadDocumentMetadata{sum:=sha256.Sum256(data);return &collectionv1.UploadDocumentMetadata{Name:"uploaded source",Filename:"원본.txt",IdempotencyKey:key,Sha256:hex.EncodeToString(sum[:]),SizeBytes:int64(len(data))}}
func noUploadJobs(t *testing.T,s *Service){t.Helper();for _,table:=range []string{"crawl_runs","operation_submissions","operations","operation_outbox"}{var n int;if e:=s.pool.QueryRow(context.Background(),"SELECT count(*) FROM "+table).Scan(&n);e!=nil||n!=0{t.Fatal("upload created a Job",table,n,e)}}}
func TestUploadConcurrentReplayAndImmutableMetadata(t *testing.T){
 s,m:=uploadFixture(t);data:=[]byte("원본\r\n  text\n");meta:=uploadMetadata(uuid.NewString(),data);ctx:=context.Background()
 var wg sync.WaitGroup;out:=make(chan *collectionv1.SourceDocument,6);errs:=make(chan error,6)
 for i:=0;i<6;i++{wg.Add(1);go func(){defer wg.Done();d,e:=s.registerUpload(ctx,meta,data);out<-d;errs<-e}()};wg.Wait();close(out);close(errs)
 for e:=range errs{if e!=nil{t.Fatal(e)}}
 var first *collectionv1.SourceDocument
 for d:=range out{if first==nil{first=d};if !proto.Equal(first,d){t.Fatal("replay changed document")}}
 if m.puts!=1||first.SourceType!="upload"||first.CrawlRunId!=""||first.OriginalFilename!=meta.Filename||first.Format!="text"||first.FormatVersion!=sourcefile.Version{t.Fatal(first,m.puts)}
 for _,field:=range []string{"name","filename","content"}{t.Run(field,func(t *testing.T){changed:=proto.Clone(meta).(*collectionv1.UploadDocumentMetadata);content:=bytes.Clone(data);switch field{case "name":changed.Name="other";case "filename":changed.Filename="other.txt";case "content":content=[]byte("changed");sum:=sha256.Sum256(content);changed.Sha256=hex.EncodeToString(sum[:]);changed.SizeBytes=int64(len(content))};if _,e:=s.registerUpload(ctx,changed,content);status.Code(e)!=codes.AlreadyExists{t.Fatal(e)}})}
 if m.puts!=1{t.Fatal("conflicts wrote storage")};noUploadJobs(t,s)
 doc,e:=s.GetDocument(ctx,&collectionv1.GetDocumentRequest{DocumentId:first.DocumentId});if e!=nil||!proto.Equal(doc,first){t.Fatal(doc,e)}
 for i:=0;i<2;i++{newMeta:=proto.Clone(meta).(*collectionv1.UploadDocumentMetadata);newMeta.IdempotencyKey=uuid.NewString();if _,e=s.registerUpload(ctx,newMeta,data);e!=nil{t.Fatal(e)}}
 page,e:=s.ListDocuments(ctx,&collectionv1.ListDocumentsRequest{Q:meta.Filename,Limit:2});if e!=nil||len(page.Items)!=2||page.NextCursor==""{t.Fatal(page,e)}
 second,e:=s.ListDocuments(ctx,&collectionv1.ListDocumentsRequest{Q:meta.Filename,Limit:2,Cursor:page.NextCursor});if e!=nil||len(second.Items)!=1||second.Items[0].DocumentId==page.Items[0].DocumentId{t.Fatal(second,e)}
}
func TestUploadStorageAndDatabaseFailuresAllowSameKeyRetry(t *testing.T){
 for _,mode:=range []string{"put","verify","database"}{t.Run(mode,func(t *testing.T){
  s,m:=uploadFixture(t);ctx:=context.Background();data:=[]byte("original");meta:=uploadMetadata(uuid.NewString(),data)
  m.fail=mode=="put";m.corrupt=mode=="verify"
  if mode=="database"{if _,e:=s.pool.Exec(ctx,`ALTER TABLE source_documents ADD CONSTRAINT upload_failure_fixture CHECK(source_type<>'upload')`);e!=nil{t.Fatal(e)}}
  if _,e:=s.registerUpload(ctx,meta,data);e==nil{t.Fatal("failure ignored")}
  for _,table:=range []string{"source_documents","api_requests"}{var n int;if e:=s.pool.QueryRow(ctx,"SELECT count(*) FROM "+table).Scan(&n);e!=nil||n!=0{t.Fatal("partial ready document",table,n,e)}}
  m.fail=false;m.corrupt=false;if mode=="database"{if _,e:=s.pool.Exec(ctx,`ALTER TABLE source_documents DROP CONSTRAINT upload_failure_fixture`);e!=nil{t.Fatal(e)}}
  if _,e:=s.registerUpload(ctx,meta,data);e!=nil{t.Fatal(e)};if len(m.objects)!=1{t.Fatal("retry leaked another object key")};noUploadJobs(t,s)
 })}
}

type uploadStream struct{grpc.ServerStream;messages []*collectionv1.UploadDocumentRequest;receiveError error;result *collectionv1.SourceDocument}
func(s *uploadStream)Context()context.Context{return context.Background()}
func(s *uploadStream)Recv()(*collectionv1.UploadDocumentRequest,error){if len(s.messages)==0{if s.receiveError!=nil{return nil,s.receiveError};return nil,io.EOF};next:=s.messages[0];s.messages=s.messages[1:];return next,nil}
func(s *uploadStream)SendAndClose(d *collectionv1.SourceDocument)error{s.result=d;return nil}
func TestUploadStreamRejectsInvalidInputBeforeStorage(t *testing.T){
 data:=[]byte("original");meta:=uploadMetadata(uuid.NewString(),data)
 cases:=map[string]*uploadStream{
  "no-header":{},
  "header-and-data":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta,Data:data}}},
  "duplicate-header":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Metadata:meta}}},
  "empty-chunk":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{}}},
  "oversize-chunk":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Data:bytes.Repeat([]byte("x"),sourcefile.ChunkBytes+1)}}},
  "truncated":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Data:data[:3]}}},
  "wrong-hash":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Data:[]byte("tampered")}}},
  "disconnected":{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Data:data}},receiveError:status.Error(codes.Canceled,"lost stream")},
 }
 for name,stream:=range cases{t.Run(name,func(t *testing.T){s:=New(nil,nil,nil);if e:=s.UploadDocument(stream);e==nil||stream.result!=nil{t.Fatal("invalid stream registered",e)}})}
}

type documentStream struct{grpc.ServerStream;data bytes.Buffer;sends int}
func(s *documentStream)Context()context.Context{return context.Background()}
func(s *documentStream)Send(c *collectionv1.DocumentChunk)error{s.sends++;_,e:=s.data.Write(c.Data);return e}
func TestUploadStreamRoundTripAndCorruptDownload(t *testing.T){
 s,m:=uploadFixture(t);data:=bytes.Repeat([]byte("a"),sourcefile.ChunkBytes+1);meta:=uploadMetadata(uuid.NewString(),data)
 stream:=&uploadStream{messages:[]*collectionv1.UploadDocumentRequest{{Metadata:meta},{Data:data[:sourcefile.ChunkBytes]},{Data:data[sourcefile.ChunkBytes:]}}}
 if e:=s.UploadDocument(stream);e!=nil{t.Fatal(e)};req:=&collectionv1.GetDocumentRequest{DocumentId:stream.result.DocumentId}
 output:=&documentStream{};if e:=s.ReadDocument(req,output);e!=nil||!bytes.Equal(output.data.Bytes(),data){t.Fatal("round trip",e)}
 for key,body:=range m.objects{body[0]='b';m.objects[key]=body}
 bad:=&documentStream{};if e:=s.ReadDocument(req,bad);status.Code(e)!=codes.DataLoss||bad.sends!=0{t.Fatal("corrupt bytes returned",e)}
 noUploadJobs(t,s)
}
func TestUploadSchemaMigratesExistingCrawlOriginal(t *testing.T){
 pool:=testsupport.Postgres(t);ctx:=context.Background()
 if _,e:=pool.Exec(ctx,Schema+idempotency.Schema+submission.Schema);e!=nil{t.Fatal(e)}
 run,op,doc:=uuid.NewString(),uuid.NewString(),uuid.NewString();data:=[]byte("legacy");sum:=sha256.Sum256(data);sha:=hex.EncodeToString(sum[:]);key,_:=blob.Key(run,sha)
 if _,e:=pool.Exec(ctx,`INSERT INTO crawl_runs(crawl_run_id,operation_id,name,source_url,options) VALUES($1,$2,'legacy','https://example.com/','{}')`,run,op);e!=nil{t.Fatal(e)}
 if _,e:=pool.Exec(ctx,`INSERT INTO source_documents(document_id,crawl_run_id,name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id) VALUES($1,$2,'legacy','https://example.com/','https://example.com/',$3,$4,'text/plain',$5,'http-source-v1','image')`,doc,run,sha,len(data),key);e!=nil{t.Fatal(e)}
 for i:=0;i<2;i++{if e:=InitSchema(ctx,pool);e!=nil{t.Fatal(e)}}
 m:=&uploadStore{objects:map[string][]byte{key:data}};s:=New(pool,nil,m)
 got,e:=s.GetDocument(ctx,&collectionv1.GetDocumentRequest{DocumentId:doc});if e!=nil||got.CrawlRunId!=run||got.SourceType!="crawl"||got.Sha256!=sha{t.Fatal("legacy document lost",got,e)}
 stream:=&documentStream{};if e=s.ReadDocument(&collectionv1.GetDocumentRequest{DocumentId:doc},stream);e!=nil||!bytes.Equal(stream.data.Bytes(),data){t.Fatal("legacy download changed",e)}
 if _,e=s.registerUpload(ctx,uploadMetadata(uuid.NewString(),[]byte("uploaded")),[]byte("uploaded"));e!=nil{t.Fatal(e)}
}
