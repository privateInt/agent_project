package app

import (
    "context"
    "testing"
    "time"

    "github.com/google/uuid"
    "github.com/jackc/pgx/v5/pgxpool"
    operationv1 "knowledge-platform/gen/operation/v1"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

func claimForGate(t *testing.T, pool *pgxpool.Pool, id string) *operationv1.Operation {
    t.Helper()
    r, err := New(pool).ClaimOperation(context.Background(), &operationv1.ClaimOperationRequest{
        OperationId: id, OwnerId: "test-dispatcher",
    })
    if err != nil || !r.GetClaimed() { t.Fatalf("claim: %v %v", r, err) }
    return r.GetOperation()
}

func expireGateLease(t *testing.T, pool *pgxpool.Pool, id string) {
    t.Helper()
    if _, err := pool.Exec(context.Background(), `UPDATE operations
        SET dispatch_lease_until=clock_timestamp()-INTERVAL '1 second'
        WHERE operation_id=$1`, id); err != nil { t.Fatal(err) }
}

func gateRequest(op *operationv1.Operation, instance string) *operationv1.BeginExecutionRequest {
    return &operationv1.BeginExecutionRequest{OperationId: op.GetOperationId(),
        DispatchToken: op.GetDispatchToken(), ExecutionInstanceId: instance,
        ResourceId: op.GetResourceId(), ExecutionType: op.GetExecutionType()}
}

func expectGateDenied(t *testing.T, err error) {
    t.Helper()
    if status.Code(err) != codes.FailedPrecondition { t.Fatalf("expected FailedPrecondition, got %v", err) }
}

func TestDispatchLeaseRecovery(t *testing.T) {
    pool := outboxTestPool(t); ctx := context.Background(); svc := New(pool)
    op := createTestOperation(t, pool)
    if op.GetDispatchProtocol()!=1 { t.Fatal("new operation is not gated") }
    first := claimForGate(t, pool, op.GetOperationId())
    if first.GetDispatchGeneration()!=1 || first.GetAttempt()!=1 || first.GetDispatchOwner()!="test-dispatcher" || first.GetDispatchLeaseUntil()==nil { t.Fatal("incomplete claim") }
    recovery := &operationv1.RecoverDispatchRequest{OperationId: first.GetOperationId(), ExpectedDispatchToken: first.GetDispatchToken()}
    _, err := svc.RecoverDispatch(ctx, recovery); expectGateDenied(t, err)
    var eventBefore, eventAfter string
    if err=pool.QueryRow(ctx, `UPDATE operation_outbox SET published_at=NOW(),publish_attempts=1
        WHERE operation_id=$1 RETURNING event_id`, op.GetOperationId()).Scan(&eventBefore); err!=nil { t.Fatal(err) }
    expireGateLease(t, pool, first.GetOperationId())
    _, err=svc.BeginExecution(ctx, gateRequest(first, uuid.NewString())); expectGateDenied(t, err)
    pending, err:=svc.RecoverDispatch(ctx, recovery); if err!=nil { t.Fatal(err) }
    if pending.GetStatus()!="pending" || pending.GetDispatchToken()!="" || pending.GetDispatchLeaseUntil()!=nil || pending.GetAttempt()!=1 { t.Fatal("recovery retained launch ownership or changed compute attempt") }
    var published bool
    if err=pool.QueryRow(ctx, `SELECT event_id,published_at IS NOT NULL FROM operation_outbox WHERE operation_id=$1`, op.GetOperationId()).Scan(&eventAfter,&published); err!=nil { t.Fatal(err) }
    if published || eventBefore!=eventAfter { t.Fatal("outbox was not rearmed with the original event") }
    second:=claimForGate(t,pool,op.GetOperationId())
    if second.GetDispatchToken()==first.GetDispatchToken() || second.GetDispatchGeneration()!=2 || second.GetAttempt()!=1 { t.Fatal("generation fencing failed") }
    _,err=svc.BeginExecution(ctx,gateRequest(first,uuid.NewString())); expectGateDenied(t,err)
    _,err=svc.RecoverDispatch(ctx,recovery); expectGateDenied(t,err)
    _,err=svc.FailDispatch(ctx,&operationv1.FailDispatchRequest{OperationId:first.GetOperationId(),DispatchToken:first.GetDispatchToken(),ErrorCode:"LATE"}); expectGateDenied(t,err)
    if _,err=svc.BeginExecution(ctx,gateRequest(second,uuid.NewString())); err!=nil { t.Fatal(err) }
}

func TestExecutionGrantAndFencedReports(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId())
    req:=gateRequest(op,uuid.NewString())
    wrong:=gateRequest(op,uuid.NewString());wrong.ResourceId=uuid.NewString()
    _,err:=svc.BeginExecution(ctx,wrong);expectGateDenied(t,err)
    wrong=gateRequest(op,uuid.NewString());wrong.ExecutionType="wrong"
    _,err=svc.BeginExecution(ctx,wrong);expectGateDenied(t,err)
    _,err=svc.MarkRunning(ctx,&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),ExecutionInstanceId:req.GetExecutionInstanceId()});expectGateDenied(t,err)
    running,err:=svc.BeginExecution(ctx,req);if err!=nil{t.Fatal(err)}
    if running.GetStatus()!="running" || running.GetExecutionStartedAt()==nil { t.Fatal("grant was not durable") }
    expireGateLease(t,pool,op.GetOperationId())
    duplicate,err:=svc.BeginExecution(ctx,req);if err!=nil{t.Fatal(err)}
    if !duplicate.GetExecutionStartedAt().AsTime().Equal(running.GetExecutionStartedAt().AsTime()) || !duplicate.GetUpdatedAt().AsTime().Equal(running.GetUpdatedAt().AsTime()) { t.Fatal("retry changed first grant") }
    _,err=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));expectGateDenied(t,err)
    _,err=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:op.GetOperationId(),ExpectedDispatchToken:op.GetDispatchToken()});expectGateDenied(t,err)
    _,err=svc.FailDispatch(ctx,&operationv1.FailDispatchRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),ErrorCode:"LATE"});expectGateDenied(t,err)
    _,err=svc.FailOperation(ctx,&operationv1.FailOperationRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),ExecutionInstanceId:uuid.NewString()});expectGateDenied(t,err)
    _,err=svc.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId(),DispatchToken:uuid.NewString(),ExecutionInstanceId:req.GetExecutionInstanceId()});expectGateDenied(t,err)
    _,err=svc.UpdatePhase(ctx,&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId(),Phase:"late"});expectGateDenied(t,err)
    completion:=&operationv1.UpdateOperationRequest{OperationId:op.GetOperationId(),DispatchToken:op.GetDispatchToken(),ExecutionInstanceId:req.GetExecutionInstanceId()}
    done,err:=svc.SucceedOperation(ctx,completion);if err!=nil||done.GetStatus()!="succeeded"{t.Fatalf("completion: %v",err)}
    again,err:=svc.SucceedOperation(ctx,completion);if err!=nil||!again.GetCompletedAt().AsTime().Equal(done.GetCompletedAt().AsTime()){t.Fatalf("completion retry: %v",err)}
    _,err=svc.BeginExecution(ctx,req);expectGateDenied(t,err)
}

func TestConcurrentExecutionGrant(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId())
    start:=make(chan struct{});results:=make(chan error,2)
    for i:=0;i<2;i++ { go func(){req:=gateRequest(op,uuid.NewString());<-start;_,err:=svc.BeginExecution(ctx,req);results<-err}() }
    close(start)
    granted,denied:=0,0
    for i:=0;i<2;i++ {err:=<-results;if err==nil{granted++}else if status.Code(err)==codes.FailedPrecondition{denied++}else{t.Fatal(err)}}
    if granted!=1||denied!=1{t.Fatalf("granted=%d denied=%d",granted,denied)}
}

func TestExpiredBeginRacesRecovery(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId());expireGateLease(t,pool,op.GetOperationId())
    start:=make(chan struct{});begin:=make(chan error,1);recoverResult:=make(chan error,1)
    go func(){<-start;_,err:=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));begin<-err}()
    go func(){<-start;_,err:=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:op.GetOperationId(),ExpectedDispatchToken:op.GetDispatchToken()});recoverResult<-err}()
    close(start);expectGateDenied(t,<-begin);if err:=<-recoverResult;err!=nil{t.Fatal(err)}
    current,err:=svc.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:op.GetOperationId()});if err!=nil{t.Fatal(err)}
    if current.GetStatus()!="pending"||current.GetExecutionStartedAt()!=nil{t.Fatal("expired process escaped recovery fence")}
}

func TestLeaseClockAfterTransactionStart(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background()
    op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId())
    tx,err:=pool.Begin(ctx);if err!=nil{t.Fatal(err)};defer tx.Rollback(ctx)
    var transactionTime time.Time
    if err=tx.QueryRow(ctx,`SELECT NOW()`).Scan(&transactionTime);err!=nil{t.Fatal(err)}
    // Give the stored lease a time after transaction start, but before this
    // next statement's wall clock. NOW() would incorrectly report it alive.
    if _,err=tx.Exec(ctx,`UPDATE operations SET dispatch_lease_until=clock_timestamp() WHERE operation_id=$1`,op.GetOperationId());err!=nil{t.Fatal(err)}
    alive,err:=leaseAlive(ctx,tx,op.GetOperationId());if err!=nil{t.Fatal(err)}
    if alive{t.Fatal("lease validation used the transaction snapshot clock")}
}

func TestDispatchRecoveryAtomicRollback(t *testing.T) {
    for _,failure:=range []string{"missing_outbox","outbox_write","operation_write"}{t.Run(failure,func(t *testing.T){
        pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
        op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId());expireGateLease(t,pool,op.GetOperationId())
        if _,err:=pool.Exec(ctx,`UPDATE operation_outbox SET published_at=NOW()`);err!=nil{t.Fatal(err)}
        var sql string
        switch failure{
        case "missing_outbox":sql=`DELETE FROM operation_outbox`
        case "outbox_write":sql=`ALTER TABLE operation_outbox ADD CONSTRAINT no_rearm CHECK(published_at IS NOT NULL)`
        case "operation_write":sql=`ALTER TABLE operations ADD CONSTRAINT no_requeue CHECK(status<>'pending')`
        }
        if _,err:=pool.Exec(ctx,sql);err!=nil{t.Fatal(err)}
        if _,err:=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:op.GetOperationId(),ExpectedDispatchToken:op.GetDispatchToken()});err==nil{t.Fatal("expected transaction failure")}
        current,err:=svc.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:op.GetOperationId()});if err!=nil{t.Fatal(err)}
        if current.GetStatus()!="dispatching"||current.GetDispatchToken()!=op.GetDispatchToken(){t.Fatal("failed recovery revoked ownership")}
        if failure!="missing_outbox"{
            var published bool
            if err=pool.QueryRow(ctx,`SELECT published_at IS NOT NULL FROM operation_outbox`).Scan(&published);err!=nil{t.Fatal(err)}
            if !published{t.Fatal("outbox escaped rolled back recovery")}
        }
    })}
}

func TestDispatchGenerationBudgetAndLegacy(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    id:=createTestOperation(t,pool).GetOperationId()
    for generation:=int32(1);generation<=3;generation++{
        op:=claimForGate(t,pool,id)
        if op.GetDispatchGeneration()!=generation||op.GetAttempt()!=1{t.Fatal("unexpected attempt accounting")}
        expireGateLease(t,pool,id)
        _,err:=svc.RecoverDispatch(ctx,&operationv1.RecoverDispatchRequest{OperationId:id,ExpectedDispatchToken:op.GetDispatchToken()})
        if generation<3{if err!=nil{t.Fatal(err)}}else{expectGateDenied(t,err)}
    }
    legacy:=createTestOperation(t,pool)
    if _,err:=pool.Exec(ctx,`UPDATE operations SET dispatch_protocol=0 WHERE operation_id=$1`,legacy.GetOperationId());err!=nil{t.Fatal(err)}
    _,err:=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OperationId:legacy.GetOperationId(),OwnerId:"test"});expectGateDenied(t,err)
    _,err=svc.ClaimOperation(ctx,&operationv1.ClaimOperationRequest{OperationId:legacy.GetOperationId()})
    if status.Code(err)!=codes.InvalidArgument{t.Fatalf("ownerless claim: %v",err)}
}

func TestReconcileRequiresExecutionGrant(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    op:=claimForGate(t,pool,createTestOperation(t,pool).GetOperationId())
    req:=&operationv1.ApplyReconciliationRequest{OperationId:op.GetOperationId(),ExpectedStatus:op.GetStatus(),ExpectedAttempt:op.GetAttempt(),ExpectedUpdatedAt:op.GetUpdatedAt(),TargetStatus:"succeeded",Note:"domain evidence"}
    _,err:=svc.ApplyReconciliation(ctx,req);expectGateDenied(t,err)
    running,err:=svc.BeginExecution(ctx,gateRequest(op,uuid.NewString()));if err!=nil{t.Fatal(err)}
    req.ExpectedStatus=running.GetStatus();req.ExpectedUpdatedAt=running.GetUpdatedAt()
    result,err:=svc.ApplyReconciliation(ctx,req);if err!=nil||result.GetStatus()!="succeeded"{t.Fatalf("authorized completion repair: %v",err)}
}
