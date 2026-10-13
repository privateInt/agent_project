package app

import (
 "context"
 "crypto/sha256"
 "encoding/hex"
 "errors"
 "io"
 "mime"
 "mime/multipart"
 "net/http"
 "time"

 collectionv1 "data-platform/gen/collection/v1"
 "data-platform/internal/idempotency"
 "data-platform/internal/sourcefile"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

// Parse the entire bounded multipart request before contacting Collection.
// This prevents a malformed trailing field from committing a document.
func readUpload(w http.ResponseWriter,r *http.Request)(*collectionv1.UploadDocumentMetadata,[]byte,error){
 key:=r.Header.Get("Idempotency-Key")
 if len(r.Header.Values("Idempotency-Key"))!=1||key==""{return nil,nil,status.Error(codes.InvalidArgument,"one Idempotency-Key header is required")}
 if e:=idempotency.ValidateKey(key);e!=nil{return nil,nil,e}
 if len(r.Header.Values("Content-Type"))!=1||r.Header.Get("Content-Encoding")!=""{return nil,nil,status.Error(codes.InvalidArgument,"one multipart Content-Type with no Content-Encoding is required")}
 kind,params,e:=mime.ParseMediaType(r.Header.Get("Content-Type"));if e!=nil||kind!="multipart/form-data"||params["boundary"]==""{return nil,nil,status.Error(codes.InvalidArgument,"multipart/form-data with name and file is required")}
 r.Body=http.MaxBytesReader(w,r.Body,sourcefile.MaxBytes+(64<<10))
 reader:=multipart.NewReader(r.Body,params["boundary"])
 meta:=&collectionv1.UploadDocumentMetadata{IdempotencyKey:key};seen:=map[string]bool{};var data []byte
 for {
  // RawPart leaves Content-Transfer-Encoding untouched; reject it explicitly.
  part,e:=reader.NextRawPart();if e==io.EOF{break};if e!=nil{return nil,nil,status.Error(codes.InvalidArgument,"invalid or oversized multipart upload")}
  dispositions:=part.Header.Values("Content-Disposition")
  disposition,p,e:=mime.ParseMediaType(part.Header.Get("Content-Disposition"))
  if e!=nil||len(dispositions)!=1||disposition!="form-data"||seen[p["name"]]||(p["name"]!="name"&&p["name"]!="file"){return nil,nil,status.Error(codes.InvalidArgument,"exactly one name field and one file field are required")}
  for field:=range p{if field!="name"&&field!="filename"{return nil,nil,status.Error(codes.InvalidArgument,"unknown multipart disposition parameter")}}
  for field:=range part.Header{if field!="Content-Disposition"&&field!="Content-Type"{return nil,nil,status.Error(codes.InvalidArgument,"unsupported multipart part header")}}
  seen[p["name"]]=true
  if p["name"]=="name" {
   if _,ok:=p["filename"];ok{return nil,nil,status.Error(codes.InvalidArgument,"name must be a text field")}
   value,e:=io.ReadAll(io.LimitReader(part,801));if e!=nil||len(value)>800{return nil,nil,status.Error(codes.InvalidArgument,"name field is too large")}
   meta.Name,e=sourcefile.Name(string(value));if e!=nil{return nil,nil,status.Error(codes.InvalidArgument,e.Error())}
  }else{
   meta.Filename=p["filename"] // Preserve/check basename before any FileName cleaning.
   if e:=sourcefile.Filename(meta.Filename);e!=nil{return nil,nil,status.Error(codes.InvalidArgument,e.Error())}
   data,e=io.ReadAll(io.LimitReader(part,sourcefile.MaxBytes+1));if e!=nil||int64(len(data))>sourcefile.MaxBytes{return nil,nil,status.Error(codes.InvalidArgument,"file exceeds the 32 MiB limit or is truncated")}
  }
  if e=part.Close();e!=nil{return nil,nil,status.Error(codes.InvalidArgument,"invalid multipart boundary")}
 }
 if !seen["name"]||!seen["file"]||len(data)==0{return nil,nil,status.Error(codes.InvalidArgument,"one nonempty name and file are required")}
 // Drain epilogue under the same overall cap; no unbounded trailing body.
 if _,e=io.Copy(io.Discard,r.Body);e!=nil{return nil,nil,status.Error(codes.InvalidArgument,"upload body exceeds the request limit")}
 sum:=sha256.Sum256(data);meta.Sha256=hex.EncodeToString(sum[:]);meta.SizeBytes=int64(len(data));return meta,data,nil
}

func(s *Server)upload(w http.ResponseWriter,r *http.Request){
 select{case s.uploads<-struct{}{}:defer func(){<-s.uploads}();default:failure(w,status.Error(codes.ResourceExhausted,"upload slots are busy"));return}
 meta,data,e:=readUpload(w,r);if e!=nil{failure(w,e);return}
 ctx,cancel:=context.WithTimeout(r.Context(),120*time.Second);defer cancel()
 stream,e:=s.Collection.UploadDocument(ctx);if e!=nil{failure(w,e);return}
 e=stream.Send(&collectionv1.UploadDocumentRequest{Metadata:meta})
 for offset:=0;e==nil&&offset<len(data);offset+=sourcefile.ChunkBytes{
  end:=offset+sourcefile.ChunkBytes;if end>len(data){end=len(data)}
  e=stream.Send(&collectionv1.UploadDocumentRequest{Data:data[offset:end]})
 }
 // Send can return EOF when the service rejects metadata; receive its status.
 if e!=nil&&!errors.Is(e,io.EOF){failure(w,e);return}
 result,e:=stream.CloseAndRecv();if e!=nil{failure(w,e);return}
 // Stable 200 for initial registration and replay; no async Operation/Job.
 write(w,http.StatusOK,documentView(result))
}
