package blob

import (
 "context"
 "fmt"
 "io"
 "knowledge-platform/internal/chunking"
 "github.com/google/uuid"
 "github.com/minio/minio-go/v7"
 "github.com/minio/minio-go/v7/pkg/credentials"
)

const Bucket = "knowledge-chunks"
type Store interface { Put(context.Context,string,io.Reader,int64,string) error; Open(context.Context,string)(io.ReadCloser,error) }
type MinIO struct{client *minio.Client}
func Connect(ctx context.Context,endpoint,key,secret string,secure bool)(*MinIO,error){
 c,e:=minio.New(endpoint,&minio.Options{Creds:credentials.NewStaticV4(key,secret,""),Secure:secure});if e!=nil{return nil,e}
 exists,e:=c.BucketExists(ctx,Bucket);if e!=nil{return nil,e}
 if !exists {if e=c.MakeBucket(ctx,Bucket,minio.MakeBucketOptions{});e!=nil{present,check:=c.BucketExists(ctx,Bucket);if check!=nil||!present{return nil,e}}}
 return &MinIO{c},nil
}
func(s *MinIO)Put(ctx context.Context,key string,r io.Reader,n int64,kind string)error{_,e:=s.client.PutObject(ctx,Bucket,key,r,n,minio.PutObjectOptions{ContentType:kind});return e}
func(s *MinIO)Open(ctx context.Context,key string)(io.ReadCloser,error){return s.client.GetObject(ctx,Bucket,key,minio.GetObjectOptions{})}
func Key(run,digest string)(string,error){id,e:=uuid.Parse(run);if e!=nil||id==uuid.Nil||id.String()!=run||!chunking.ValidDigest(digest){return "",fmt.Errorf("invalid result identity")};return "runs/"+run+"/sha256/"+digest+"/chunks.json",nil}
func Read(ctx context.Context,s Store,key,digest string,size int64)([]byte,error){
 if size<1||size>chunking.MaxOutputBytes||!chunking.ValidDigest(digest){return nil,fmt.Errorf("invalid result size or hash")}
 r,e:=s.Open(ctx,key);if e!=nil{return nil,e};defer r.Close()
 raw,e:=io.ReadAll(io.LimitReader(r,size+1));if e!=nil{return nil,e}
 if int64(len(raw))!=size||chunking.Digest(raw)!=digest{return nil,fmt.Errorf("result checksum mismatch")};return raw,nil
}
