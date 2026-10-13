package app

import (
 "bytes"
 "context"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "io"
 "mime"
 "mime/multipart"
 "net"
 "net/http/httptest"
 "net/textproto"
 "strings"
 "sync"
 "testing"

 collectionv1 "data-platform/gen/collection/v1"
 "data-platform/internal/sourcefile"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/credentials/insecure"
 "google.golang.org/grpc/status"
 "google.golang.org/grpc/test/bufconn"
)

type uploadPart struct{name,filename,body string;headers textproto.MIMEHeader}
func multipartBody(t *testing.T,parts []uploadPart)([]byte,string){
 t.Helper();var b bytes.Buffer;writer:=multipart.NewWriter(&b)
 for _,p:=range parts{
  fields:=map[string]string{"name":p.name};if p.filename!=""{fields["filename"]=p.filename}
  header:=textproto.MIMEHeader{"Content-Disposition":[]string{mime.FormatMediaType("form-data",fields)}}
  for k,v:=range p.headers{header[k]=v}
  part,e:=writer.CreatePart(header);if e!=nil{t.Fatal(e)};if _,e=io.WriteString(part,p.body);e!=nil{t.Fatal(e)}
 }
 if e:=writer.Close();e!=nil{t.Fatal(e)};return b.Bytes(),writer.FormDataContentType()
}
type uploadRPC struct{collectionv1.UnimplementedCollectionServiceServer;mu sync.Mutex;calls int;meta *collectionv1.UploadDocumentMetadata;data []byte;early error}
func(f *uploadRPC)UploadDocument(s collectionv1.CollectionService_UploadDocumentServer)error{
 f.mu.Lock();defer f.mu.Unlock();f.calls++;if f.early!=nil{return f.early}
 first,e:=s.Recv();if e!=nil{return e};f.meta=first.Metadata;var b bytes.Buffer
 for{part,e:=s.Recv();if e==io.EOF{break};if e!=nil{return e};if len(part.Data)>sourcefile.ChunkBytes{return status.Error(codes.InvalidArgument,"chunk too large")};b.Write(part.Data)}
 f.data=bytes.Clone(b.Bytes())
 return s.SendAndClose(&collectionv1.SourceDocument{DocumentId:"75a0cc24-2ec3-4ddd-b1ee-d8204d25cf56",Name:f.meta.Name,SourceType:"upload",OriginalFilename:f.meta.Filename,Format:"text",FormatVersion:sourcefile.Version,Sha256:f.meta.Sha256,SizeBytes:f.meta.SizeBytes,State:"ready"})
}
func uploadClient(t *testing.T,f *uploadRPC)collectionv1.CollectionServiceClient{
 t.Helper();listener:=bufconn.Listen(1<<20);server:=grpc.NewServer();collectionv1.RegisterCollectionServiceServer(server,f)
 go func(){_ = server.Serve(listener)}()
 t.Cleanup(func(){server.Stop();_ = listener.Close()})
 conn,e:=grpc.NewClient("passthrough:///test-upload",grpc.WithTransportCredentials(insecure.NewCredentials()),grpc.WithContextDialer(func(context.Context,string)(net.Conn,error){return listener.Dial()}));if e!=nil{t.Fatal(e)}
 t.Cleanup(func(){_ = conn.Close()});return collectionv1.NewCollectionServiceClient(conn)
}
func TestMultipartInvalidTailNeverContactsCollection(t *testing.T){
 base:=[]uploadPart{{name:"name",body:"document"},{name:"file",filename:"a.txt",body:"original"}}
 cases:=map[string][]uploadPart{
  "missing-file":{base[0]},"missing-name":{base[1]},
  "duplicate-name":{base[0],base[1],base[0]},"duplicate-file":{base[0],base[1],base[1]},
  "unknown-after-file":{base[0],base[1],{name:"extra",body:"trailing"}},
  "traversal":{base[0],{name:"file",filename:"../a.txt",body:"text"}},
  "empty":{base[0],{name:"file",filename:"a.txt"}},
  "name-file":{{name:"name",filename:"a.txt",body:"doc"},base[1]},
  "encoding":{base[0],{name:"file",filename:"a.txt",body:"text",headers:textproto.MIMEHeader{"Content-Transfer-Encoding":[]string{"base64"}}}},
 }
 handler:=New(nil,nil).Handler()
 for name,parts:=range cases{t.Run(name,func(t *testing.T){body,kind:=multipartBody(t,parts);r:=httptest.NewRequest("POST","/api/source-documents",bytes.NewReader(body));r.Header.Set("Content-Type",kind);r.Header.Set("Idempotency-Key","upload-test");w:=httptest.NewRecorder();handler.ServeHTTP(w,r);if w.Code!=400{t.Fatal(w.Code,w.Body.String())}})}
 for _,mode:=range []string{"keyless","duplicate-key","invalid-key","missing-type","truncated","oversized"}{t.Run(mode,func(t *testing.T){
  parts:=base;if mode=="oversized"{parts=[]uploadPart{base[0],{name:"file",filename:"a.txt",body:strings.Repeat("a",int(sourcefile.MaxBytes)+1)}}}
  body,kind:=multipartBody(t,parts);if mode=="truncated"{body=body[:len(body)-8]}
  r:=httptest.NewRequest("POST","/api/source-documents",bytes.NewReader(body));r.Header.Set("Content-Type",kind);r.Header.Set("Idempotency-Key","key")
  switch mode{case "keyless":r.Header.Del("Idempotency-Key");case "duplicate-key":r.Header.Add("Idempotency-Key","other");case "invalid-key":r.Header.Set("Idempotency-Key","../bad");case "missing-type":r.Header.Del("Content-Type")}
  w:=httptest.NewRecorder();handler.ServeHTTP(w,r);if w.Code!=400{t.Fatal(w.Code,w.Body.String())}
 })}
}
func TestMultipartStreamsChunksAndReturnsUploadProvenance(t *testing.T){
 f:=&uploadRPC{};handler:=New(uploadClient(t,f),nil).Handler();original:=strings.Repeat("원본 \r\n",10000)
 body,kind:=multipartBody(t,[]uploadPart{{name:"file",filename:"원본.txt",body:original},{name:"name",body:" source "}})
 r:=httptest.NewRequest("POST","/api/source-documents",bytes.NewReader(body));r.Header.Set("Content-Type",kind);r.Header.Set("Idempotency-Key","upload-once");w:=httptest.NewRecorder();handler.ServeHTTP(w,r)
 if w.Code!=200{t.Fatal(w.Code,w.Body.String())}
 f.mu.Lock();defer f.mu.Unlock();sum:=sha256.Sum256([]byte(original))
 if f.calls!=1||string(f.data)!=original||f.meta.Name!="source"||f.meta.IdempotencyKey!="upload-once"||f.meta.Sha256!=hex.EncodeToString(sum[:]){t.Fatal("transport changed input",f.meta)}
 var doc map[string]any;if e:=json.Unmarshal(w.Body.Bytes(),&doc);e!=nil{t.Fatal(e)}
 if doc["source_type"]!="upload"||doc["original_filename"]!="원본.txt"||doc["format"]!="text"||doc["format_version"]!=sourcefile.Version||doc["crawl_run_id"]!=""{t.Fatal(doc)}
}
func TestEarlyUploadRPCRejectionIsReturnedWithoutRetry(t *testing.T){
 f:=&uploadRPC{early:status.Error(codes.AlreadyExists,"key already used")};handler:=New(uploadClient(t,f),nil).Handler()
 body,kind:=multipartBody(t,[]uploadPart{{name:"name",body:"doc"},{name:"file",filename:"a.txt",body:strings.Repeat("a",2<<20)}})
 r:=httptest.NewRequest("POST","/api/source-documents",bytes.NewReader(body));r.Header.Set("Content-Type",kind);r.Header.Set("Idempotency-Key","same-key");w:=httptest.NewRecorder();handler.ServeHTTP(w,r)
 if w.Code!=409{t.Fatal(w.Code,w.Body.String())};f.mu.Lock();defer f.mu.Unlock();if f.calls!=1{t.Fatal("automatic upload retry")}
}
