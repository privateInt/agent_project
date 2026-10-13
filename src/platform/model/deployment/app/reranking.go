package app

import (
    "context"
    "net/http"
    "time"

    serving "model-platform/deployment/runtime"
    deploymentv1 "model-platform/gen/deployment/v1"
    rerankingv1 "model-platform/gen/reranking/v1"
    managementv1 "model-platform/gen/management/v1"
    "model-platform/internal/reranking"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

type RerankingServer struct {
    rerankingv1.UnimplementedRerankingServiceServer
    Service *Service
    HTTP *http.Client
}

func rerankerReady(dep *deploymentv1.Deployment) error {
    spec:=dep.GetServing()
    if dep.GetDeletedAt()!=nil || dep.GetStatus()!="ready" || dep.GetRuntimeState()!="ready" || dep.GetDesiredState()!="running" || spec.GetEngine()!="vllm" || dep.GetArtifactKind()!="snapshot" ||
        spec.GetProfile()!=serving.RerankerProfile {
        return status.Error(codes.FailedPrecondition,"RERANK_NOT_READY: ready snapshot reranker deployment required")
    }
    if spec.GetMaxModelLen()!=512 { return status.Error(codes.FailedPrecondition,"RERANK_UNSUPPORTED_PROFILE: token limit must be 512") }
    return nil
}

func (s *RerankingServer) resolve(ctx context.Context, requested string) (*rerankingv1.RerankerProfile,error) {
    id,err:=deploymentID(requested)
    if err!=nil || id!=requested { return nil,status.Error(codes.InvalidArgument,"RERANK_INVALID_REQUEST: canonical deployment UUID required") }
    if s.Service==nil || s.Service.runtime==nil { return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: serving is not configured") }
    dep,err:=s.Service.GetDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:id})
    if err!=nil{return nil,err}
    if err=rerankerReady(dep);err!=nil{return nil,err}
    if dep.GetDeploymentId()!=id || dep.GetInternalEndpoint()!="http://model-serving-"+id+":8000/v1" || dep.GetServedModelName()!="deployment-"+id { return nil,status.Error(codes.FailedPrecondition,"RERANK_IDENTITY_MISMATCH: unexpected runtime endpoint") }
    conn,err:=s.Service.pool.Acquire(ctx)
    if err!=nil{return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: deployment lookup failed")}
    row,err:=loadServing(ctx,conn,id)
    conn.Release()
    if err!=nil{return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: serving plan unavailable")}
    if row.plan.Source.Adapter!=nil || row.plan.Source.ModelVersionID!=dep.GetModelVersionId() || row.plan.ImageID!=dep.GetRuntimeImageId() || row.containerID!=dep.GetRuntimeContainerId() || !row.authorized || row.desired!="running" || row.state!="ready" || row.plan.Spec.MaxModelLen!=dep.GetServing().GetMaxModelLen() || row.plan.Spec.Profile!=dep.GetServing().GetProfile() {
        return nil,status.Error(codes.FailedPrecondition,"RERANK_IDENTITY_MISMATCH: serving plan changed")
    }
    version,err:=s.Service.clients.Management.GetModelVersion(ctx,&managementv1.GetModelVersionRequest{ModelVersionId:dep.ModelVersionId})
    if err!=nil{return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: model version lookup failed")}
    source:=row.plan.Source
    if version.GetModelVersionId()!=source.ModelVersionID || version.GetRepoId()!=serving.RerankerRepository || version.GetArtifactKind()!="snapshot" || version.GetSourceType()!="huggingface" || version.GetArtifactSha256()!=source.SHA256 || version.GetArtifactUri()!=source.URI || version.GetArtifactSizeBytes()!=source.SizeBytes || version.GetResolvedRevision()=="" {
        return nil,status.Error(codes.FailedPrecondition,"RERANK_UNSUPPORTED_PROFILE: pinned artifact does not match ko-reranker-v1")
    }
    model,err:=s.Service.clients.Management.GetModel(ctx,&managementv1.GetModelRequest{ModelId:version.GetModelId()})
    if err!=nil{return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: model lookup failed")}
    if model.GetModelId()!=version.GetModelId() || (model.GetModelType()!="reranker" && model.GetModelType()!="llm") { return nil,status.Error(codes.FailedPrecondition,"RERANK_UNSUPPORTED_PROFILE: incompatible registered model type") }
    // Inspect validates plan hash, ownership, image, network and read-only mount.
    evidence,err:=s.Service.runtime.driver.Inspect(ctx,row.plan)
    if err!=nil || evidence==nil || evidence.ID!=dep.GetRuntimeContainerId() || !evidence.State.Running || evidence.State.Paused || evidence.State.Restarting {
        return nil,status.Error(codes.Unavailable,"RERANK_UNAVAILABLE: owned runtime is not running")
    }
    tokenizer,err:=serving.ValidateRerankerSnapshot(s.Service.runtime.config.CacheDir,row.plan)
    if err!=nil{return nil,status.Error(codes.FailedPrecondition,"RERANK_UNSUPPORTED_PROFILE: "+err.Error())}
    profile:=&rerankingv1.RerankerProfile{
        DeploymentId:id,ModelVersionId:dep.ModelVersionId,ProfileId:serving.RerankerProfile,
        SourceManifestSha256:source.SHA256,TokenizerSha256:tokenizer,
        RuntimeImageId:dep.RuntimeImageId,RuntimeContainerId:dep.RuntimeContainerId,ServedModelName:dep.ServedModelName,
        RepoId:version.RepoId,ResolvedRevision:version.ResolvedRevision,RegisteredModelType:model.ModelType,
        ScoreKind:"logit",ScoreDirection:"higher_is_better",PairPolicy:reranking.PairPolicy,
        MaxInputTokens:512,MaxCandidates:reranking.MaxCandidates,MaxInputBytes:reranking.MaxInputBytes,
        Truncation:"reject",Capability:"reranker",Status:"ready",ServingProfile:dep.Serving.Profile,
    }
    profile.RerankerProfileId=reranking.Identity(profile)
    return profile,nil
}

func (s *RerankingServer) GetProfile(ctx context.Context,req *rerankingv1.GetProfileRequest)(*rerankingv1.RerankerProfile,error){
    ctx,cancel:=context.WithTimeout(ctx,20*time.Second);defer cancel()
    return s.resolve(ctx,req.GetDeploymentId())
}

func (s *RerankingServer) Rerank(ctx context.Context,req *rerankingv1.RerankRequest)(*rerankingv1.RerankResponse,error){
    ctx,cancel:=context.WithTimeout(ctx,120*time.Second);defer cancel()
    if err:=reranking.ValidateRequest(req);err!=nil{return nil,err}
    profile,err:=s.resolve(ctx,req.GetDeploymentId())
    if err!=nil{return nil,err}
    client:=s.HTTP
    if client==nil{client=reranking.NewHTTPClient()}
    result,err:=reranking.Infer(ctx,client,"http://model-serving-"+profile.DeploymentId+":8000",profile,req)
    if err!=nil{return nil,err}
    // A stop/delete while inference is in flight must not yield accepted scores.
    current,err:=s.Service.GetDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:profile.DeploymentId})
    if err!=nil{return nil,err}
    if err=rerankerUnchanged(profile,current);err!=nil{return nil,err}
    return result,nil
}

func rerankerUnchanged(profile *rerankingv1.RerankerProfile,current *deploymentv1.Deployment)error{
    if err:=rerankerReady(current);err!=nil{return err}
    if current.GetRuntimeContainerId()!=profile.RuntimeContainerId || current.GetRuntimeImageId()!=profile.RuntimeImageId || current.GetModelVersionId()!=profile.ModelVersionId || current.GetServing().GetMaxModelLen()!=profile.MaxInputTokens || current.GetServing().GetProfile()!=profile.ServingProfile {
        return status.Error(codes.FailedPrecondition,"RERANK_IDENTITY_MISMATCH: deployment changed during inference")
    }
    if current.GetDeploymentId()!=profile.DeploymentId || current.GetInternalEndpoint()!="http://model-serving-"+profile.DeploymentId+":8000/v1" || current.GetServedModelName()!=profile.ServedModelName {return status.Error(codes.FailedPrecondition,"RERANK_IDENTITY_MISMATCH: inference identity changed")}
    return nil
}
