package app

import (
 "bytes"
 "context"
 "crypto/sha256"
 "encoding/hex"
 "io"
 "time"

 collectionv1 "data-platform/gen/collection/v1"
 "data-platform/internal/blob"
 "data-platform/internal/idempotency"
 "data-platform/internal/sourcefile"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
)

// Existing source rows remain crawl originals with their existing object keys.
// The constraint is added once inside the startup schema transaction.
const uploadSchema = `
ALTER TABLE source_documents ALTER COLUMN crawl_run_id DROP NOT NULL;
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS source_type TEXT NOT NULL DEFAULT 'crawl';
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS original_filename TEXT NOT NULL DEFAULT '';
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS format TEXT NOT NULL DEFAULT '';
ALTER TABLE source_documents ADD COLUMN IF NOT EXISTS format_version TEXT NOT NULL DEFAULT '';
DO $$ BEGIN
 IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conrelid='source_documents'::regclass AND conname='source_documents_origin_check') THEN
  ALTER TABLE source_documents ADD CONSTRAINT source_documents_origin_check CHECK (
   (source_type='crawl' AND crawl_run_id IS NOT NULL AND original_filename='' AND format='' AND format_version='') OR
   (source_type='upload' AND crawl_run_id IS NULL AND source_url='' AND final_url='' AND original_filename<>'' AND size_bytes>0
    AND format IN ('text','markdown','html','pdf','png','jpeg','docx','xlsx') AND format_version<>'')
  );
 END IF;
END $$;
`

func normalizeUpload(m *collectionv1.UploadDocumentMetadata)(*collectionv1.UploadDocumentMetadata,error){
 if m==nil{return nil,status.Error(codes.InvalidArgument,"upload metadata must be the first message")}
 name,e:=sourcefile.Name(m.Name);if e!=nil{return nil,status.Error(codes.InvalidArgument,e.Error())}
 if e=sourcefile.Filename(m.Filename);e!=nil{return nil,status.Error(codes.InvalidArgument,e.Error())}
 if m.IdempotencyKey==""{return nil,status.Error(codes.InvalidArgument,"Idempotency-Key is required")}
 if e=idempotency.ValidateKey(m.IdempotencyKey);e!=nil{return nil,e}
 if m.SizeBytes<1||m.SizeBytes>sourcefile.MaxBytes||!blob.ValidDigest(m.Sha256){return nil,status.Error(codes.InvalidArgument,"upload size must be 1..33554432 bytes with a lowercase SHA-256")}
 normalized:=proto.Clone(m).(*collectionv1.UploadDocumentMetadata);normalized.Name=name;return normalized,nil
}

func(s *Service)UploadDocument(stream collectionv1.CollectionService_UploadDocumentServer)error{
 ctx:=stream.Context()
 select{case s.uploads<-struct{}{}:defer func(){<-s.uploads}();default:return status.Error(codes.ResourceExhausted,"upload slots are busy; retry with the same Idempotency-Key")}
 first,e:=stream.Recv();if e!=nil{return status.Error(codes.InvalidArgument,"upload metadata is missing")}
 if len(first.GetData())!=0{return status.Error(codes.InvalidArgument,"first upload message must contain metadata only")}
 meta,e:=normalizeUpload(first.GetMetadata());if e!=nil{return e}
 var body bytes.Buffer;body.Grow(int(meta.SizeBytes))
 for {
  part,e:=stream.Recv();if e==io.EOF{break};if e!=nil{return e}
  if part.GetMetadata()!=nil||len(part.GetData())==0||len(part.GetData())>sourcefile.ChunkBytes||int64(body.Len()+len(part.GetData()))>meta.SizeBytes{return status.Error(codes.InvalidArgument,"invalid upload chunk or declared size exceeded")}
  body.Write(part.Data)
 }
 result,e:=s.registerUpload(ctx,meta,body.Bytes());if e!=nil{return e};return stream.SendAndClose(result)
}

func(s *Service)registerUpload(ctx context.Context,meta *collectionv1.UploadDocumentMetadata,data []byte)(*collectionv1.SourceDocument,error){
 normalized,e:=normalizeUpload(meta);if e!=nil{return nil,e};meta=normalized
 sum:=sha256.Sum256(data)
 if int64(len(data))!=meta.SizeBytes||hex.EncodeToString(sum[:])!=meta.Sha256{return nil,status.Error(codes.InvalidArgument,"uploaded bytes do not match declared size and SHA-256")}
 info,e:=sourcefile.Recognize(ctx,meta.Filename,data);if e!=nil{if ctx.Err()!=nil{return nil,status.FromContextError(ctx.Err()).Err()};return nil,status.Error(codes.InvalidArgument,e.Error())}
 fingerprint,e:=idempotency.Fingerprint(struct{Version,Name,Filename,SHA,Format string;Size int64}{"upload-request-v1",meta.Name,meta.Filename,meta.Sha256,info.Format,meta.SizeBytes},nil);if e!=nil{return nil,e}
 keySum:=sha256.Sum256([]byte(meta.IdempotencyKey));key,_:=blob.UploadKey(hex.EncodeToString(keySum[:]),meta.Sha256)
 out:=&collectionv1.SourceDocument{}
 e=idempotency.ExecuteWithTimeout(ctx,s.pool,"collection.upload",meta.IdempotencyKey,fingerprint,out,90*time.Second,func(ctx context.Context,tx pgx.Tx)error{
  // The per-key transaction serializes writers. A failed commit can leave only
  // an unregistered object; the same request reuses its exact key on retry.
  if e:=s.store.Put(ctx,key,bytes.NewReader(data),meta.SizeBytes,info.ContentType);e!=nil{return status.Error(codes.Unavailable,"source upload failed; retry with the same Idempotency-Key")}
  if e:=blob.Verify(ctx,s.store,key,meta.Sha256,meta.SizeBytes);e!=nil{return status.Error(codes.DataLoss,"stored upload integrity verification failed")}
  id:=uuid.NewString()
  _,e:=tx.Exec(ctx,`INSERT INTO source_documents(document_id,crawl_run_id,name,source_url,final_url,sha256,size_bytes,content_type,object_key,processor_version,processor_image_id,source_type,original_filename,format,format_version)
   VALUES($1,NULL,$2,'','',$3,$4,$5,$6,'upload-source-v1','','upload',$7,$8,$9)`,id,meta.Name,meta.Sha256,meta.SizeBytes,info.ContentType,key,meta.Filename,info.Format,sourcefile.Version)
  if e!=nil{return e}
  doc,e:=s.scanDoc(tx.QueryRow(ctx,docSelect+` WHERE document_id=$1`,id));if e!=nil{return e};proto.Merge(out,doc);return nil
 })
 if e!=nil{return nil,e};return out,nil
}
