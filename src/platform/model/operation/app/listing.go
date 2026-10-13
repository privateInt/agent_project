package app

import (
    "context"
    operationv1 "model-platform/gen/operation/v1"
    "model-platform/internal/idempotency"
    "model-platform/internal/listing"
)

func (s *Service) ListOperations(ctx context.Context, req *operationv1.ListOperationsRequest) (*operationv1.ListOperationsResponse,error) {
    state,err:=listing.Enum(req.GetStatus(),"status","pending","dispatching","running","retry_wait","cancelling","succeeded","failed","cancelled");if err!=nil{return nil,err}
    resourceType,err:=listing.Enum(req.GetResourceType(),"resource_type","model_version","training_run","deployment");if err!=nil{return nil,err}
    resourceID,err:=listing.UUID(req.GetResourceId(),"resource_id");if err!=nil{return nil,err}
    kind,err:=listing.Text(req.GetKind());if err!=nil{return nil,err}
    phase,err:=listing.Text(req.GetPhase());if err!=nil{return nil,err}
    page,err:=listing.New(req.GetLimit(),req.GetCursor(),"operations",state,resourceType,resourceID,kind,phase);if err!=nil{return nil,err}
    query:=listing.Query{}
    query.Equal("status",state);query.Equal("resource_type",resourceType);query.Equal("resource_id",resourceID);query.Equal("kind",kind);query.Equal("phase",phase)
    suffix:=query.Finish(page,"created_at","operation_id")
    rows,err:=s.pool.Query(ctx,operationSelect+suffix,query.Args...)
    if err!=nil{return nil,idempotency.DBError(err)};defer rows.Close()
    out:=&operationv1.ListOperationsResponse{}
    for rows.Next(){
        if len(out.Operations)==page.Limit {last:=out.Operations[len(out.Operations)-1];out.NextCursor=page.Next(last.CreatedAt.AsTime(),last.OperationId);break}
        op,err:=scanOperation(rows);if err!=nil{return nil,idempotency.DBError(err)}
        // A browsing response never carries an execution grant.
        op.DispatchToken="";op.DispatchOwner="";op.ExecutionInstanceId=""
        out.Operations=append(out.Operations,op)
    }
    return out,idempotency.DBError(rows.Err())
}
