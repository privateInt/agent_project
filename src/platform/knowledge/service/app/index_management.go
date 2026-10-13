package app
import (
 "context"
 indexingv1 "knowledge-platform/gen/indexing/v1"
 "knowledge-platform/internal/idempotency"
)
func(s *IndexService)Activate(ctx context.Context,r *indexingv1.MutationRequest)(*indexingv1.JSONResponse,error){
 if e:=validID(r.GetIndexId());e!=nil{return nil,e};if e:=validID(r.GetIndexVersionId());e!=nil{return nil,e};if r.ExpectedRevision<1{return nil,invalid("expected_revision is required")}
 tx,e:=s.chunks.pool.Begin(ctx);if e!=nil{return nil,indexError(e)};defer idempotency.Rollback(tx)
 idx,e:=scanIndex(tx.QueryRow(ctx,indexSelect+` WHERE index_id=$1 FOR UPDATE`,r.IndexId));if e!=nil{return nil,indexError(e)}
 if idx.State!="active"||idx.Revision!=r.ExpectedRevision||idx.ActiveVersionID!=r.ExpectedActiveVersionId{return nil,precondition("INDEX_ACTIVATION_CONFLICT: reload current index")}
 v,e:=scanVersion(tx.QueryRow(ctx,versionSelect+` WHERE index_version_id=$1 FOR UPDATE`,r.IndexVersionId));if e!=nil{return nil,indexError(e)};if v.IndexID!=idx.ID||v.State!="ready"||v.ReferenceState!="held"{return nil,precondition("only a ready version of this index can be activated")}
 if idx.ActiveVersionID!=v.ID{idx.ActiveVersionID=v.ID;idx.Revision++;if _,e=tx.Exec(ctx,`UPDATE logical_indexes SET active_version_id=$2,revision=$3 WHERE index_id=$1`,idx.ID,idx.ActiveVersionID,idx.Revision);e!=nil{return nil,indexError(e)}}
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};return jsonIndex(idx),nil
}
func(s *IndexService)DeleteVersion(ctx context.Context,r *indexingv1.VersionRequest)(*indexingv1.JSONResponse,error){
 if e:=validID(r.GetIndexVersionId());e!=nil{return nil,e}
 // Read parent identity without lock, then always lock parent -> version.
 var parent string;if e:=s.chunks.pool.QueryRow(ctx,`SELECT index_id::text FROM index_versions WHERE index_version_id=$1`,r.IndexVersionId).Scan(&parent);e!=nil{return nil,indexError(e)}
 tx,e:=s.chunks.pool.Begin(ctx);if e!=nil{return nil,indexError(e)};defer idempotency.Rollback(tx)
 idx,e:=scanIndex(tx.QueryRow(ctx,indexSelect+` WHERE index_id=$1 FOR UPDATE`,parent));if e!=nil{return nil,indexError(e)}
 v,e:=scanVersion(tx.QueryRow(ctx,versionSelect+` WHERE index_version_id=$1 FOR UPDATE`,r.IndexVersionId));if e!=nil{return nil,indexError(e)}
 if v.State=="deleted"||v.State=="deleting"{return jsonIndex(v),nil};if idx.ActiveVersionID==v.ID{return nil,precondition("ACTIVE_VERSION: activate another ready version or delete the whole index")}
 if _,e=tx.Exec(ctx,`UPDATE index_versions SET state='deleting',error_code='',error_message='',updated_at=now() WHERE index_version_id=$1`,v.ID);e!=nil{return nil,indexError(e)}
 if _,e=tx.Exec(ctx,`UPDATE logical_indexes SET revision=revision+1 WHERE index_id=$1`,parent);e!=nil{return nil,indexError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};v.State="deleting";return jsonIndex(v),nil
}
func(s *IndexService)DeleteIndex(ctx context.Context,r *indexingv1.MutationRequest)(*indexingv1.JSONResponse,error){
 if e:=validID(r.GetIndexId());e!=nil{return nil,e};if r.ExpectedRevision<1{return nil,invalid("expected_revision is required")}
 tx,e:=s.chunks.pool.Begin(ctx);if e!=nil{return nil,indexError(e)};defer idempotency.Rollback(tx)
 idx,e:=scanIndex(tx.QueryRow(ctx,indexSelect+` WHERE index_id=$1 FOR UPDATE`,r.IndexId));if e!=nil{return nil,indexError(e)}
 if idx.State!="active"{return jsonIndex(idx),nil};if idx.Revision!=r.ExpectedRevision||idx.ActiveVersionID!=r.ExpectedActiveVersionId{return nil,precondition("INDEX_DELETE_CONFLICT: reload current index")}
 if _,e=tx.Exec(ctx,`UPDATE logical_indexes SET state='deleting',active_version_id='',revision=revision+1 WHERE index_id=$1`,idx.ID);e!=nil{return nil,indexError(e)}
 if _,e=tx.Exec(ctx,`UPDATE index_versions SET state='deleting',error_code='',error_message='',updated_at=now() WHERE index_id=$1 AND state NOT IN ('deleting','deleted')`,idx.ID);e!=nil{return nil,indexError(e)}
 if e=tx.Commit(ctx);e!=nil{return nil,indexError(e)};idx.State="deleting";idx.ActiveVersionID="";idx.Revision++;return jsonIndex(idx),nil
}
