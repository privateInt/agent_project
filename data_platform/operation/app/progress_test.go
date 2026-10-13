package app

import (
    "context"
    "testing"
    operationv1 "data-platform/gen/operation/v1"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/proto"
)

func sampleProgress() *operationv1.TransferProgress {
    return &operationv1.TransferProgress{Stage:"downloading",CurrentFile:"model.safetensors",BytesCompleted:5,BytesTotal:10,FilesTotal:2,Sequence:1}
}
func TestTransferProgressValidation(t *testing.T) {
    for _, c := range []struct{name string; change func(*operationv1.TransferProgress)}{
        {"negative",func(p *operationv1.TransferProgress){p.BytesCompleted=-1}},
        {"overflow",func(p *operationv1.TransferProgress){p.BytesCompleted=11}},
        {"files",func(p *operationv1.TransferProgress){p.FilesCompleted=3}},
        {"path",func(p *operationv1.TransferProgress){p.CurrentFile="../outside"}},
        {"stage",func(p *operationv1.TransferProgress){p.Stage="succeeded"}},
        {"sequence",func(p *operationv1.TransferProgress){p.Sequence=0}},
    } { t.Run(c.name,func(t *testing.T){p:=sampleProgress();c.change(p);if status.Code(validateTransferProgress(p))!=codes.InvalidArgument{t.Fatal("accepted invalid progress")}}) }
    p:=sampleProgress();p.BytesTotal=0
    if err:=validateTransferProgress(p);err!=nil{t.Fatal("unknown HTTP length rejected",err)}
}

func TestTransferProgressFencingPersistenceAndTerminalHistory(t *testing.T) {
    pool:=outboxTestPool(t);ctx:=context.Background();svc:=New(pool)
    created,err:=svc.CreateOperation(ctx,&operationv1.CreateOperationRequest{Kind:"model.import.huggingface",ExecutionType:"management.import-huggingface",ResourceType:"model_version",ResourceId:uuid.NewString()})
    if err!=nil{t.Fatal(err)}
    claimed:=claimForGate(t,pool,created.GetOperationId())
    grant:=gateRequest(claimed,uuid.NewString())
    if _,err=svc.BeginExecution(ctx,grant);err!=nil{t.Fatal(err)}
    req:=&operationv1.UpdateOperationRequest{OperationId:claimed.GetOperationId(),Phase:"transferring_files",DispatchToken:grant.GetDispatchToken(),ExecutionInstanceId:grant.GetExecutionInstanceId(),Progress:sampleProgress()}
    bad:=proto.Clone(req).(*operationv1.UpdateOperationRequest);bad.ExecutionInstanceId=uuid.NewString()
    _,err=svc.UpdatePhase(ctx,bad);expectGateDenied(t,err)
    first,err:=svc.UpdatePhase(ctx,req)
    if err!=nil||first.GetProgressUpdatedAt()==nil||!proto.Equal(first.GetProgress(),req.GetProgress()){t.Fatalf("save: %v %v",first,err)}
    again,err:=svc.UpdatePhase(ctx,req)
    if err!=nil||!proto.Equal(again.GetProgressUpdatedAt(),first.GetProgressUpdatedAt()){t.Fatal("duplicate sample changed timestamps",err)}
    req.Progress.Sequence=3;req.Progress.BytesCompleted=8
    latest,err:=svc.UpdatePhase(ctx,req);if err!=nil{t.Fatal(err)}
    req.Progress.Sequence=2;req.Progress.BytesCompleted=6
    stale,err:=svc.UpdatePhase(ctx,req)
    if err!=nil||stale.GetProgress().GetBytesCompleted()!=8||!proto.Equal(stale.GetProgressUpdatedAt(),latest.GetProgressUpdatedAt()){t.Fatal("older sample replaced latest",err)}
    loaded,err:=svc.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:req.OperationId})
    if err!=nil||loaded.GetProgress().GetSequence()!=3{t.Fatal("progress not readable",err)}
    if _,err=svc.UpdatePhase(ctx,&operationv1.UpdateOperationRequest{OperationId:req.OperationId,Phase:"writing_manifest",DispatchToken:req.DispatchToken,ExecutionInstanceId:req.ExecutionInstanceId});err!=nil{t.Fatal(err)}
    req.Progress.Sequence=4
    _,err=svc.UpdatePhase(ctx,req);expectGateDenied(t,err)
    done,err:=svc.FailOperation(ctx,&operationv1.FailOperationRequest{OperationId:req.OperationId,DispatchToken:req.DispatchToken,ExecutionInstanceId:req.ExecutionInstanceId,ErrorCode:"TRANSFER_FAILED",ErrorMessage:"synthetic upload failure"})
    if err!=nil{t.Fatal(err)}
    req.Progress.Sequence=4
    _,err=svc.UpdatePhase(ctx,req);expectGateDenied(t,err)
    after,err:=svc.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:req.OperationId})
    if err!=nil||!proto.Equal(after,done){t.Fatal("late progress changed failed history",err)}
}
