package app

import (
    "context"
    "time"
    serving "model-platform/deployment/runtime"
    deploymentv1 "model-platform/gen/deployment/v1"
    "model-platform/internal/idempotency"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

// Keep the row and Operation references for history and response-loss replay.
// Only a stopped deployment's owned runtime and private cache are removed.
func (s *Service) DeleteDeployment(ctx context.Context, req *deploymentv1.GetDeploymentRequest) (*deploymentv1.Deployment,error) {
    id,err:=deploymentID(req.GetDeploymentId());if err!=nil { return nil,err }
    conn,err:=s.pool.Acquire(ctx);if err!=nil { return nil,idempotency.DBError(err) };defer conn.Release()
    var locked bool
    if err=conn.QueryRow(ctx,`SELECT pg_try_advisory_lock(hashtextextended($1::text,0))`,"serving:"+id).Scan(&locked);err!=nil { return nil,idempotency.DBError(err) }
    if !locked { return nil,status.Error(codes.FailedPrecondition,"deployment is being reconciled; retry shortly") }
    defer func(){
        cleanup,cancel:=context.WithTimeout(context.Background(),2*time.Second);defer cancel()
        var released bool
        if e:=conn.QueryRow(cleanup,`SELECT pg_advisory_unlock(hashtextextended($1::text,0))`,"serving:"+id).Scan(&released);e!=nil || !released { _=conn.Conn().Close(cleanup) }
    }()
    current,err:=s.get(ctx,id);if err!=nil { return nil,err }
    if current.GetDeletedAt()!=nil { return current,nil }
    if current.GetStatus()!="stopped" || current.GetDesiredState()!="stopped" || current.GetRuntimeState()!="stopped" || current.GetServing().GetEngine()!="vllm" {
        return nil,status.Error(codes.FailedPrecondition,"stop this serving deployment and wait for confirmed stopped state first")
    }
    if s.runtime==nil { return nil,status.Error(codes.Unavailable,"runtime cleanup is unavailable") }
    remover,ok:=s.runtime.driver.(interface{ RemoveStopped(context.Context,serving.Plan,string) error })
    purger,canPurge:=s.runtime.fetcher.(interface{ Purge(context.Context,string) error })
    if !ok || !canPurge { return nil,status.Error(codes.Unavailable,"runtime cleanup is unavailable") }
    row,err:=loadServing(ctx,conn,id);if err!=nil { return nil,idempotency.DBError(err) }
    evidence,err:=s.runtime.driver.Inspect(ctx,row.plan)
    if err!=nil { return nil,status.Error(codes.FailedPrecondition,"runtime identity could not be verified") }
    if evidence!=nil && (row.containerID=="" || evidence.ID!=row.containerID || evidence.State.Running || evidence.State.Paused || evidence.State.Restarting || (evidence.State.Status!="exited" && evidence.State.Status!="created")) {
        return nil,status.Error(codes.FailedPrecondition,"runtime identity or stopped state changed")
    }
    // Release any remaining GPU reservation before forgetting the runtime.
    if releaser,ok:=s.runtime.driver.(interface{ ReleaseGPU(context.Context,serving.Plan) error });ok {
        if err=releaser.ReleaseGPU(ctx,row.plan);err!=nil { return nil,status.Error(codes.FailedPrecondition,"GPU release unconfirmed; retry deletion") }
    }
    if err=remover.RemoveStopped(ctx,row.plan,row.containerID);err!=nil { return nil,status.Error(codes.FailedPrecondition,"runtime removal unconfirmed; inspect and retry deletion") }
    if err=purger.Purge(ctx,id);err!=nil { return nil,status.Error(codes.FailedPrecondition,"deployment cache removal unconfirmed; retry deletion") }
    // A crash before this commit leaves a stopped, retryable item in the list.
    tag,err:=conn.Exec(ctx,`UPDATE deployments SET deleted_at=clock_timestamp(),updated_at=clock_timestamp(),runtime_endpoint='' WHERE deployment_id=$1 AND deleted_at IS NULL AND desired_state='stopped' AND runtime_state='stopped' AND status='stopped'`,id)
    if err!=nil { return nil,idempotency.DBError(err) }
    if tag.RowsAffected()!=1 { return nil,status.Error(codes.FailedPrecondition,"deployment changed during removal") }
    return s.get(ctx,id)
}
