package app
import("context";"testing";"strings";"time";refinementv1 "data-platform/gen/refinement/v1";operationv1 "data-platform/gen/operation/v1";"github.com/google/uuid";"google.golang.org/grpc/codes";"google.golang.org/grpc/status")
func TestKnowledgeReferencesProtectSharedCanonicalUntilAllOwnersRelease(t *testing.T){
 s,ops,m,_:=fixture(t);ctx:=context.Background();r:=prepare(t,s,ops,m);run,e:=s.CompleteRun(ctx,r);if e!=nil{t.Fatal(e)};store:=&purgeCanonicalStore{memoryStore:m};s.store=store
 if _,e=ops.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:r.OperationId,DispatchToken:r.DispatchToken,ExecutionInstanceId:r.ExecutionInstanceId,Phase:"completed"});e!=nil{t.Fatal(e)}
 doc,e:=s.GetDocument(ctx,&refinementv1.GetDocumentRequest{DocumentId:run.OutputDocumentId});if e!=nil{t.Fatal(e)}
 a,b:=uuid.NewString(),uuid.NewString();request:=func(id string)*refinementv1.KnowledgeReferencesRequest{return &refinementv1.KnowledgeReferencesRequest{IndexVersionId:id,Documents:[]*refinementv1.CanonicalPin{{CanonicalId:doc.DocumentId,Sha256:doc.Sha256}}}}
 first,e:=s.AcquireKnowledgeReferences(ctx,request(a));if e!=nil||first.State!="held"{t.Fatal(e)};again,e:=s.AcquireKnowledgeReferences(ctx,request(a));if e!=nil||again.Fingerprint!=first.Fingerprint{t.Fatal("acquire replay changed",e)}
 changed:=request(a);changed.Documents[0].Sha256=strings.Repeat("f",64);if _,e=s.AcquireKnowledgeReferences(ctx,changed);status.Code(e)!=codes.FailedPrecondition{t.Fatal("owner pins mutated",e)}
 if _,e=s.AcquireKnowledgeReferences(ctx,request(b));e!=nil{t.Fatal(e)};target:=&refinementv1.GetRunRequest{RefinementRunId:run.RefinementRunId}
 if _,e=s.PurgeRun(ctx,target);status.Code(e)!=codes.FailedPrecondition||store.deletes!=0{t.Fatal("referenced canonical purged",e)}
 if _,e=s.ReleaseKnowledgeReferences(ctx,&refinementv1.KnowledgeReferenceOwner{IndexVersionId:a});e!=nil{t.Fatal(e)}
 if _,e=s.PurgeRun(ctx,target);status.Code(e)!=codes.FailedPrecondition||store.deletes!=0{t.Fatal("shared reference lost",e)}
 if _,e=s.AcquireKnowledgeReferences(ctx,request(a));status.Code(e)!=codes.FailedPrecondition{t.Fatal("released owner resurrected",e)}
 if _,e=s.ReleaseKnowledgeReferences(ctx,&refinementv1.KnowledgeReferenceOwner{IndexVersionId:b});e!=nil{t.Fatal(e)}
 if _,e=s.PurgeRun(ctx,target);e!=nil||store.deletes!=1{t.Fatal("released canonical not purgeable",e)}
 if _,e=s.AcquireKnowledgeReferences(ctx,request(uuid.NewString()));status.Code(e)!=codes.FailedPrecondition{t.Fatal("purged canonical referenced",e)}
}
func TestReleaseBeforeAcquireCreatesTombstone(t *testing.T){s,_,_,_:=fixture(t);id:=uuid.NewString();ctx:=context.Background();for i:=0;i<2;i++{out,e:=s.ReleaseKnowledgeReferences(ctx,&refinementv1.KnowledgeReferenceOwner{IndexVersionId:id});if e!=nil||out.State!="released"{t.Fatal(e)}};_,e:=s.AcquireKnowledgeReferences(ctx,&refinementv1.KnowledgeReferencesRequest{IndexVersionId:id,Documents:[]*refinementv1.CanonicalPin{{CanonicalId:uuid.NewString(),Sha256:strings.Repeat("a",64)}}});if status.Code(e)!=codes.FailedPrecondition{t.Fatal("delayed acquire escaped tombstone",e)}}
func TestReferenceAcquireSerializesWithPermanentPurgeFence(t *testing.T){
 s,ops,m,_:=fixture(t);ctx:=context.Background();r:=prepare(t,s,ops,m);run,e:=s.CompleteRun(ctx,r);if e!=nil{t.Fatal(e)};doc,e:=s.GetDocument(ctx,&refinementv1.GetDocumentRequest{DocumentId:run.OutputDocumentId});if e!=nil{t.Fatal(e)}
 tx,e:=s.pool.Begin(ctx);if e!=nil{t.Fatal(e)};defer tx.Rollback(ctx)
 if _,e=tx.Exec(ctx,`SELECT pg_advisory_xact_lock(hashtextextended($1::text,0))`,"purge-files:"+run.RefinementRunId);e!=nil{t.Fatal(e)}
 done:=make(chan error,1);go func(){_,e:=s.AcquireKnowledgeReferences(ctx,&refinementv1.KnowledgeReferencesRequest{IndexVersionId:uuid.NewString(),Documents:[]*refinementv1.CanonicalPin{{CanonicalId:doc.DocumentId,Sha256:doc.Sha256}}});done<-e}()
 select{case e:=<-done:t.Fatal("acquire bypassed purge lock",e);case<-time.After(30*time.Millisecond):}
 if _,e=tx.Exec(ctx,`UPDATE refinement_runs SET purge_state='purging' WHERE refinement_run_id=$1`,run.RefinementRunId);e!=nil{t.Fatal(e)};if e=tx.Commit(ctx);e!=nil{t.Fatal(e)}
 select{case e:=<-done:if status.Code(e)!=codes.FailedPrecondition{t.Fatal("purging fence ignored",e)};case<-time.After(5*time.Second):t.Fatal("reference stayed blocked")}
}
