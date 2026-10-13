package app
import (
 "context"
 "errors"
 "strings"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5"
 operationv1 "knowledge-platform/gen/operation/v1"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

func(s *Service)ListReconcileOperations(ctx context.Context,r *operationv1.ListReconcileOperationsRequest)(*operationv1.ListReconcileOperationsResponse,error){
 limit:=r.GetLimit();if limit<=0||limit>100 {limit=50}
 rows,err:=s.pool.Query(ctx,operationSelect+` WHERE status IN ('dispatching','running') AND updated_at<NOW()-INTERVAL '60 seconds'
 AND (reconcile_checked_at IS NULL OR reconcile_checked_at<NOW()-INTERVAL '30 seconds')
 ORDER BY reconcile_checked_at NULLS FIRST,updated_at,operation_id LIMIT $1`,limit)
 if err!=nil{return nil,status.Errorf(codes.Internal,"list reconciliation candidates: %v",err)};defer rows.Close()
 result:=&operationv1.ListReconcileOperationsResponse{}
 for rows.Next(){op,e:=scanOperation(rows);if e!=nil{return nil,status.Errorf(codes.Internal,"scan candidate: %v",e)};result.Operations=append(result.Operations,op)}
 if err=rows.Err();err!=nil{return nil,status.Errorf(codes.Internal,"read candidates: %v",err)}
 return result,nil
}

// Compare-and-swap protects against a Job completing while evidence is read.
// This RPC repairs observations only; RecoverDispatch handles unstarted leases.
func(s *Service)ApplyReconciliation(ctx context.Context,r *operationv1.ApplyReconciliationRequest)(*operationv1.Operation,error){
 if _,err:=uuid.Parse(r.GetOperationId());err!=nil{return nil,status.Error(codes.InvalidArgument,"valid operation_id required")}
 if r.GetExpectedUpdatedAt()==nil||r.GetExpectedUpdatedAt().CheckValid()!=nil{return nil,status.Error(codes.InvalidArgument,"expected_updated_at required")}
 target:=r.GetTargetStatus()
 if target!=""&&target!="running"&&target!="succeeded"&&target!="failed"{return nil,status.Error(codes.InvalidArgument,"unsupported reconciliation target")}
 note:=strings.TrimSpace(r.GetNote());if note==""||len(note)>2000{return nil,status.Error(codes.InvalidArgument,"note must contain 1..2000 bytes")}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Errorf(codes.Internal,"begin reconciliation: %v",err)};defer tx.Rollback(ctx)
 op,err:=scanOperation(tx.QueryRow(ctx,operationSelect+` WHERE operation_id=$1 FOR UPDATE`,r.GetOperationId()))
 if errors.Is(err,pgx.ErrNoRows){return nil,status.Error(codes.NotFound,"operation not found")}
 if err!=nil{return nil,status.Errorf(codes.Internal,"lock reconciliation: %v",err)}
 if op.GetStatus()!=r.GetExpectedStatus()||op.GetAttempt()!=r.GetExpectedAttempt()||!op.GetUpdatedAt().AsTime().Equal(r.GetExpectedUpdatedAt().AsTime()){
  return nil,status.Error(codes.FailedPrecondition,"operation changed during observation")
 }
 if op.GetStatus()!="dispatching"&&op.GetStatus()!="running"{return nil,status.Error(codes.FailedPrecondition,"operation is not active")}
 if target!="" && op.GetDispatchProtocol()==1 && op.GetExecutionStartedAt()==nil {return nil,status.Error(codes.FailedPrecondition,"execution has not been authorized")}
 if target==""||target==op.GetStatus(){
  _,err=tx.Exec(ctx,`UPDATE operations SET reconcile_checked_at=NOW(),reconcile_note=$2 WHERE operation_id=$1`,r.GetOperationId(),note)
 }else{
  _,err=tx.Exec(ctx,`UPDATE operations SET status=$2::varchar,phase=$3,reconcile_checked_at=NOW(),reconcile_note=$4,
 updated_at=NOW(),completed_at=CASE WHEN $2::varchar IN ('succeeded','failed') THEN NOW() ELSE completed_at END,
 error_code=CASE WHEN $2::varchar='failed' THEN 'DOMAIN_REPORTED_FAILURE' ELSE '' END,
 error_message=CASE WHEN $2::varchar='failed' THEN $4::text ELSE '' END,retryable=FALSE
 WHERE operation_id=$1`,r.GetOperationId(),target,"reconciled_"+target,note)
 }
 if err!=nil{return nil,status.Errorf(codes.Internal,"write reconciliation: %v",err)}
 op,err=scanOperation(tx.QueryRow(ctx,operationSelect+` WHERE operation_id=$1`,r.GetOperationId()));if err!=nil{return nil,status.Errorf(codes.Internal,"read reconciliation: %v",err)}
 if err=tx.Commit(ctx);err!=nil{return nil,status.Errorf(codes.Internal,"commit reconciliation: %v",err)}
 return op,nil
}
