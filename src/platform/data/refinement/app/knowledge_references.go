package app

import (
 "context"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "sort"
 "strings"
 refinementv1 "data-platform/gen/refinement/v1"
 "data-platform/internal/idempotency"
 "github.com/jackc/pgx/v5"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

const knowledgeReferenceSchema = `
CREATE TABLE IF NOT EXISTS knowledge_reference_owners (
 index_version_id UUID PRIMARY KEY, state TEXT NOT NULL CHECK(state IN ('held','released')),
 fingerprint TEXT NOT NULL, documents_json BYTEA NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS knowledge_canonical_references (
 index_version_id UUID NOT NULL REFERENCES knowledge_reference_owners(index_version_id),
 canonical_id UUID NOT NULL REFERENCES canonical_documents(document_id),
 PRIMARY KEY(index_version_id,canonical_id)
);
CREATE INDEX IF NOT EXISTS knowledge_canonical_refs_idx ON knowledge_canonical_references(canonical_id);
`
func referencePins(r *refinementv1.KnowledgeReferencesRequest)([]*refinementv1.CanonicalPin,[]byte,string,error){
 if e:=validID(r.GetIndexVersionId());e!=nil{return nil,nil,"",e}
 if len(r.GetDocuments())<1||len(r.GetDocuments())>20{return nil,nil,"",status.Error(codes.InvalidArgument,"choose 1..20 pinned documents")}
 pins:=append([]*refinementv1.CanonicalPin(nil),r.Documents...);sort.Slice(pins,func(i,j int)bool{return pins[i].GetCanonicalId()<pins[j].GetCanonicalId()})
 for i,p:=range pins{if e:=validID(p.GetCanonicalId());e!=nil{return nil,nil,"",e};b,e:=hex.DecodeString(p.GetSha256());if e!=nil||len(b)!=32||strings.ToLower(p.GetSha256())!=p.GetSha256()||i>0&&pins[i-1].CanonicalId==p.CanonicalId{return nil,nil,"",status.Error(codes.InvalidArgument,"invalid/duplicate canonical pin")}}
 raw,_:=json.Marshal(pins);h:=sha256.Sum256(raw);return pins,raw,hex.EncodeToString(h[:]),nil
}
func referenceOwnerLock(ctx context.Context,tx pgx.Tx,id string)error{_,e:=tx.Exec(ctx,`SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))`,"knowledge-reference:"+id);return e}
func (s *Service) AcquireKnowledgeReferences(ctx context.Context,r *refinementv1.KnowledgeReferencesRequest)(*refinementv1.KnowledgeReferences,error){
 pins,raw,fp,e:=referencePins(r);if e!=nil{return nil,e}
 tx,e:=s.pool.Begin(ctx);if e!=nil{return nil,idempotency.DBError(e)};defer idempotency.Rollback(tx)
 if e=referenceOwnerLock(ctx,tx,r.IndexVersionId);e!=nil{return nil,idempotency.DBError(e)}
 var state,old string
 e=tx.QueryRow(ctx,`SELECT state,fingerprint FROM knowledge_reference_owners WHERE index_version_id=$1`,r.IndexVersionId).Scan(&state,&old)
 if e==nil{if state!="held"||old!=fp{return nil,status.Error(codes.FailedPrecondition,"REFERENCE_CONFLICT: owner was released or pins differ")};return &refinementv1.KnowledgeReferences{IndexVersionId:r.IndexVersionId,State:state,Fingerprint:fp,Documents:pins},nil}
 if e!=pgx.ErrNoRows{return nil,idempotency.DBError(e)}
 // Sort actual refinement IDs to avoid deadlocks for multi-document acquisition.
 runs:=map[string]string{};ids:=[]string{}
 for _,p:=range pins{var run string;if e=tx.QueryRow(ctx,`SELECT refinement_run_id::text FROM canonical_documents WHERE document_id=$1`,p.CanonicalId).Scan(&run);e!=nil{return nil,readError(e)};if _,ok:=runs[run];!ok{ids=append(ids,run)};runs[run]=p.CanonicalId}
 sort.Strings(ids)
 for _,id:=range ids{if _,e=tx.Exec(ctx,`SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))`,"purge-files:"+id);e!=nil{return nil,idempotency.DBError(e)}}
 for _,p:=range pins{var ok bool;e=tx.QueryRow(ctx,`SELECT EXISTS(SELECT 1 FROM canonical_documents c JOIN refinement_runs r ON r.refinement_run_id=c.refinement_run_id WHERE c.document_id=$1 AND c.sha256=$2 AND r.purged_at IS NULL AND r.purge_state='' AND r.deleted_at IS NULL)`,p.CanonicalId,p.Sha256).Scan(&ok);if e!=nil{return nil,idempotency.DBError(e)};if !ok{return nil,status.Error(codes.FailedPrecondition,"CANONICAL_UNAVAILABLE: reference pins cannot be protected")}}
 if _,e=tx.Exec(ctx,`INSERT INTO knowledge_reference_owners(index_version_id,state,fingerprint,documents_json) VALUES($1,'held',$2,$3)`,r.IndexVersionId,fp,raw);e!=nil{return nil,idempotency.DBError(e)}
 for _,p:=range pins{if _,e=tx.Exec(ctx,`INSERT INTO knowledge_canonical_references VALUES($1,$2)`,r.IndexVersionId,p.CanonicalId);e!=nil{return nil,idempotency.DBError(e)}}
 if e=tx.Commit(ctx);e!=nil{return nil,idempotency.DBError(e)}
 return &refinementv1.KnowledgeReferences{IndexVersionId:r.IndexVersionId,State:"held",Fingerprint:fp,Documents:pins},nil
}
func(s *Service) ReleaseKnowledgeReferences(ctx context.Context,r *refinementv1.KnowledgeReferenceOwner)(*refinementv1.KnowledgeReferences,error){
 if e:=validID(r.GetIndexVersionId());e!=nil{return nil,e}
 tx,e:=s.pool.Begin(ctx);if e!=nil{return nil,idempotency.DBError(e)};defer idempotency.Rollback(tx)
 if e=referenceOwnerLock(ctx,tx,r.IndexVersionId);e!=nil{return nil,idempotency.DBError(e)}
 // Tombstone even an absent owner: delayed acquisition must never resurrect it.
 if _,e=tx.Exec(ctx,`INSERT INTO knowledge_reference_owners(index_version_id,state,fingerprint,documents_json) VALUES($1,'released','','[]'::bytea) ON CONFLICT(index_version_id) DO UPDATE SET state='released'`,r.IndexVersionId);e!=nil{return nil,idempotency.DBError(e)}
 if _,e=tx.Exec(ctx,`DELETE FROM knowledge_canonical_references WHERE index_version_id=$1`,r.IndexVersionId);e!=nil{return nil,idempotency.DBError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,idempotency.DBError(e)};return s.GetKnowledgeReferences(ctx,r)
}
func(s *Service) GetKnowledgeReferences(ctx context.Context,r *refinementv1.KnowledgeReferenceOwner)(*refinementv1.KnowledgeReferences,error){
 if e:=validID(r.GetIndexVersionId());e!=nil{return nil,e};out:=&refinementv1.KnowledgeReferences{IndexVersionId:r.IndexVersionId};var raw []byte
 if e:=s.pool.QueryRow(ctx,`SELECT state,fingerprint,documents_json FROM knowledge_reference_owners WHERE index_version_id=$1`,r.IndexVersionId).Scan(&out.State,&out.Fingerprint,&raw);e!=nil{return nil,readError(e)}
 if json.Unmarshal(raw,&out.Documents)!=nil{return nil,status.Error(codes.DataLoss,"reference receipt invalid")};return out,nil
}
