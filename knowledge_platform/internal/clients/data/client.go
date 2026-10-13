// Package data uses Data's gRPC APIs, never its database or bucket credentials.
package data

import (
 "context"
 "fmt"
 "io"
 "knowledge-platform/internal/chunking"
 collectionv1 "knowledge-platform/gen/data/collection/v1"
 refinementv1 "knowledge-platform/gen/data/refinement/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)
type Client struct {Collection collectionv1.CollectionServiceClient;Refinement refinementv1.RefinementServiceClient}
func(c Client)Resolve(ctx context.Context,id string)(chunking.Input,error){
 d,e:=c.Refinement.GetDocument(ctx,&refinementv1.GetDocumentRequest{DocumentId:id});if e!=nil{return chunking.Input{},e}
 run,e:=c.Refinement.GetRun(ctx,&refinementv1.GetRunRequest{RefinementRunId:d.GetRefinementRunId()});if e!=nil{return chunking.Input{},e}
 src,e:=c.Collection.GetDocument(ctx,&collectionv1.GetDocumentRequest{DocumentId:d.GetSourceDocumentId()});if e!=nil{return chunking.Input{},e}
 if d.GetDocumentId()!=id || run.GetOutputDocumentId()!=id || run.GetSourceDocumentId()!=src.GetDocumentId() || run.GetSourceSha256()!=src.GetSha256() || run.GetDeletedAt()!=nil || run.GetPurgeState()!="" || src.GetDeletedAt()!=nil || src.GetPurgeState()!="" || src.GetState()!="ready" || d.GetSchemaVersion()!="canonical-document-v1" || d.GetRulesVersion()!=run.GetRulesVersion() || d.GetSizeBytes()<1 || d.GetSizeBytes()>chunking.MaxInputBytes {return chunking.Input{},status.Error(codes.FailedPrecondition,"selected canonical/source is not available")}
 return chunking.Input{CanonicalID:id,CanonicalSHA256:d.Sha256,SizeBytes:d.SizeBytes,SourceDocumentID:src.DocumentId,SourceSHA256:src.Sha256,RefinementRunID:run.RefinementRunId,SchemaVersion:d.SchemaVersion,ProcessorVersion:d.ProcessorVersion,RulesVersion:d.RulesVersion,ProcessorImageID:d.ProcessorImageId,Format:run.SourceFormat},nil
}
func(c Client)Read(ctx context.Context,in chunking.Input)([]byte,error){
 // Recheck selected identities, never resolve latest. Preview has no cross-
 // platform retention lock yet; purged inputs fail explicitly.
 current,e:=c.Resolve(ctx,in.CanonicalID);if e!=nil{return nil,e};if current!=in{return nil,fmt.Errorf("input manifest changed")}
 stream,e:=c.Refinement.ReadDocument(ctx,&refinementv1.GetDocumentRequest{DocumentId:in.CanonicalID});if e!=nil{return nil,e}
 raw:=make([]byte,0,int(in.SizeBytes))
 for {part,e:=stream.Recv();if e==io.EOF{break};if e!=nil{return nil,e};b:=part.GetData();if len(b)==0||len(b)>64<<10||int64(len(raw)+len(b))>in.SizeBytes{return nil,fmt.Errorf("canonical stream exceeds declared size")};raw=append(raw,b...)}
 if int64(len(raw))!=in.SizeBytes||chunking.Digest(raw)!=in.CanonicalSHA256{return nil,fmt.Errorf("canonical stream checksum mismatch")};return raw,nil
}
