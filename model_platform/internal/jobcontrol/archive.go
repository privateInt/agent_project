package jobcontrol

import (
 "context"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "fmt"
 "io"
 "path"
 "strings"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "github.com/minio/minio-go/v7"
 "github.com/minio/minio-go/v7/pkg/credentials"
)

type ArchiveStore interface{Manifests(context.Context,string)([]string,error);Open(context.Context,string)(io.ReadCloser,error)}
type ArchiveReader struct{Store ArchiveStore}
type s3Archive struct{client *minio.Client;bucket string}
func NewArchive(endpoint,access,secret,bucket string,ssl bool)(*ArchiveReader,error){
 client,err:=minio.New(endpoint,&minio.Options{Creds:credentials.NewStaticV4(access,secret,""),Secure:ssl});if err!=nil{return nil,err}
 return &ArchiveReader{Store:&s3Archive{client,bucket}},nil // never create a bucket for a read API
}
func(s *s3Archive)Manifests(ctx context.Context,prefix string)([]string,error){
 ctx,cancel:=context.WithCancel(ctx);defer cancel()
 var out []string;count:=0
 for obj:=range s.client.ListObjects(ctx,s.bucket,minio.ListObjectsOptions{Prefix:prefix,Recursive:true}){
  if obj.Err!=nil{if minio.ToErrorResponse(obj.Err).Code=="NoSuchBucket"{return nil,nil};return nil,fmt.Errorf("archive listing unavailable")}
  count++;if count>200{return nil,fmt.Errorf("archive listing limit exceeded")}
  if strings.HasSuffix(obj.Key,"/manifest.json"){out=append(out,obj.Key)}
 }
 return out,nil
}
func(s *s3Archive)Open(ctx context.Context,key string)(io.ReadCloser,error){return s.client.GetObject(ctx,s.bucket,key,minio.GetObjectOptions{})}
type entry struct{Path string `json:"path"`;Size int64 `json:"size"`;SHA256 string `json:"sha256"`}
func hash(data []byte)string{sum:=sha256.Sum256(data);return hex.EncodeToString(sum[:])}
func(a *ArchiveReader)small(ctx context.Context,key string,limit int64)([]byte,error){
 r,err:=a.Store.Open(ctx,key);if err!=nil{return nil,err};defer r.Close()
 data,err:=io.ReadAll(io.LimitReader(r,limit+1));if err!=nil||int64(len(data))>limit{return nil,fmt.Errorf("archive object invalid or too large")};return data,nil
}
func(a *ArchiveReader)Read(ctx context.Context,op *operationv1.Operation,lines,max int)(*trainingv1.TrainingLogs,error){
 if op.GetStatus()!="succeeded"&&op.GetStatus()!="failed"&&op.GetStatus()!="cancelled"{return nil,nil}
 prefix:="operations/"+op.GetOperationId()+"/"+op.GetDispatchToken()+"/"
 keys,err:=a.Store.Manifests(ctx,prefix);if err!=nil{return nil,err};if len(keys)==0{return nil,nil}
 if len(keys)!=1{return nil,fmt.Errorf("ambiguous log archive; explicit review required")}
 key:=keys[0];parts:=strings.Split(strings.TrimPrefix(key,prefix),"/")
 if !strings.HasPrefix(key,prefix)||len(parts)!=3||!hexID.MatchString(parts[0])||!hexID.MatchString(parts[1])||parts[2]!="manifest.json"{return nil,fmt.Errorf("archive path mismatch")}
 raw,err:=a.small(ctx,key,1<<20);if err!=nil{return nil,err}
 if !equalDigest(hash(raw),parts[1]){return nil,fmt.Errorf("archive manifest digest mismatch")}
 var manifest struct{SchemaVersion int `json:"schema_version"`;Kind string `json:"kind"`;Files []entry `json:"files"`}
 if json.Unmarshal(raw,&manifest)!=nil||manifest.SchemaVersion!=1||manifest.Kind!="job-logs"||len(manifest.Files)!=3{return nil,fmt.Errorf("invalid archive manifest")}
 files:=map[string]entry{}
 for _,f:=range manifest.Files{
  if (f.Path!="metadata.json"&&f.Path!="stdout.log"&&f.Path!="stderr.log")||f.Size<0||f.Size>128<<20||!hexID.MatchString(f.SHA256){return nil,fmt.Errorf("invalid archive entry")}
  if _,exists:=files[f.Path];exists{return nil,fmt.Errorf("duplicate archive entry")};files[f.Path]=f
 }
 base:=path.Dir(key)+"/"
 metadata,err:=a.small(ctx,base+"metadata.json",1<<20);if err!=nil{return nil,err}
 f:=files["metadata.json"];if int64(len(metadata))!=f.Size||hash(metadata)!=f.SHA256{return nil,fmt.Errorf("archive metadata digest mismatch")}
 var meta struct{SchemaVersion int `json:"schema_version"`;OperationID string `json:"operation_id"`;DispatchToken string `json:"dispatch_token"`;ExecutionInstanceID string `json:"execution_instance_id"`;ResourceID string `json:"resource_id"`;ResourceType string `json:"resource_type"`;ExecutionType string `json:"execution_type"`;ContainerID string `json:"container_id"`;Status string `json:"status"`}
 if json.Unmarshal(metadata,&meta)!=nil||meta.SchemaVersion!=1||meta.OperationID!=op.GetOperationId()||meta.DispatchToken!=op.GetDispatchToken()||meta.ExecutionInstanceID!=op.GetExecutionInstanceId()||meta.ResourceID!=op.GetResourceId()||meta.ResourceType!="training_run"||meta.ExecutionType!="training.run"||meta.ContainerID!=parts[0]||meta.Status!=op.GetStatus(){return nil,fmt.Errorf("archive execution identity mismatch")}
 out:=&trainingv1.TrainingLogs{TrainingRunId:op.GetResourceId(),OperationId:op.GetOperationId(),Source:"archive",Availability:"available",TailLines:int32(lines),MaxBytes:int32(max)}
 for _,name:=range []string{"stdout.log","stderr.log"}{
  f:=files[name];r,err:=a.Store.Open(ctx,base+name);if err!=nil{return nil,err}
  tail:=&tailBuffer{max:max};digest:=sha256.New()
  n,err:=io.Copy(io.MultiWriter(tail,digest),io.LimitReader(r,f.Size+1));closeErr:=r.Close()
  if err!=nil||closeErr!=nil||n!=f.Size||hex.EncodeToString(digest.Sum(nil))!=f.SHA256{return nil,fmt.Errorf("archive log digest mismatch")}
  text:=textUTF8(tail.lines(lines));if name=="stdout.log"{out.Stdout=text}else{out.Stderr=text};out.Truncated=out.Truncated||tail.clipped
 }
 return out,nil
}
