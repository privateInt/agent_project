package main

import (
    "context"
    "log"
    "time"

    deploymentv1 "model-platform/gen/deployment/v1"
    operationv1 "model-platform/gen/operation/v1"
    "model-platform/internal/operationreport"
    "google.golang.org/grpc"
    "google.golang.org/grpc/credentials/insecure"
)

func runServing() {
    ctx,cancel:=context.WithTimeout(context.Background(),50*time.Minute);defer cancel()
    id,resource:=required("OPERATION_ID"),required("RESOURCE_ID")
    oc,err:=grpc.NewClient(env("OPERATION_GRPC_ADDRESS","operation:9000"),grpc.WithTransportCredentials(insecure.NewCredentials()))
    if err!=nil {log.Print(err);return};defer oc.Close()
    ops:=operationreport.New(operationv1.NewOperationServiceClient(oc))
    if err:=ops.Authorize(ctx,id,resource,"deployment.serve");err!=nil {log.Printf("serving execution not authorized: %v",err);return}
    dc,err:=grpc.NewClient(env("DEPLOYMENT_GRPC_ADDRESS","deployment:9000"),grpc.WithTransportCredentials(insecure.NewCredentials()))
    if err!=nil {log.Print(err);return};defer dc.Close()
    deploy:=deploymentv1.NewDeploymentServiceClient(dc)
    token,instance:=ops.ExecutionIdentity()
    request:=&deploymentv1.StartServingRequest{DeploymentId:resource,OperationId:id,DispatchToken:token,ExecutionInstanceId:instance}
    accepted:=false
    lastPhase:=""
    for ctx.Err()==nil {
        if !accepted {
            callCtx,callCancel:=context.WithTimeout(ctx,10*time.Second)
            _,err=deploy.StartServing(callCtx,request);callCancel()
            accepted=err==nil
            // StartServing persists its authorization and is safe to replay with
            // the same process identity after a lost response.
        }
        callCtx,callCancel:=context.WithTimeout(ctx,5*time.Second)
        state,lookupErr:=deploy.GetDeployment(callCtx,&deploymentv1.GetDeploymentRequest{DeploymentId:resource});callCancel()
        if lookupErr==nil && state.GetOperationId()==id {
            switch state.GetApplyStatus() {
            case "ready":
                _,err:=ops.SucceedOperation(ctx,&operationv1.UpdateOperationRequest{OperationId:id,Phase:"completed"})
                if err!=nil {log.Printf("serving ready; completion report unconfirmed: %v",err)} else {log.Printf("serving ready deployment=%s endpoint=%s model=%s",resource,state.GetEndpoint(),state.GetServedModelName())}
                return
            case "failed":
                code,message:=state.GetRuntimeErrorCode(),state.GetRuntimeErrorMessage()
                if code=="" {code="DEPLOYMENT_STOPPED";message="deployment was stopped before becoming ready"}
                _,err:=ops.FailOperation(ctx,&operationv1.FailOperationRequest{OperationId:id,Phase:"failed",ErrorCode:code,ErrorMessage:message,Retryable:false})
                if err!=nil {log.Printf("serving domain failed; operation report unconfirmed: %v",err)}
                return
            }
            phase:="serving_"+state.GetRuntimeState()
            if phase!=lastPhase {
                if _,err:=ops.UpdatePhase(ctx,&operationv1.UpdateOperationRequest{OperationId:id,Phase:phase});err==nil {lastPhase=phase}
            }
        }
        select {case <-ctx.Done():case <-time.After(2*time.Second):}
    }
    // A transport timeout does not prove provisioning failed. The Deployment
    // controller and Operation Reconciler retain the authoritative outcome.
    log.Printf("serving coordinator stopped without a confirmed domain result deployment=%s",resource)
}
