package app
import (
    "testing"
    serving "model-platform/deployment/runtime"
    deploymentv1 "model-platform/gen/deployment/v1"
    rerankingv1 "model-platform/gen/reranking/v1"
    "google.golang.org/protobuf/proto"
    "google.golang.org/protobuf/types/known/timestamppb"
)
func TestRerankerReadyRequiresExplicitProfileAndStableRuntime(t *testing.T){
    d:=&deploymentv1.Deployment{DeploymentId:"d",Status:"ready",RuntimeState:"ready",DesiredState:"running",ArtifactKind:"snapshot",ModelVersionId:"v",RuntimeContainerId:"c",RuntimeImageId:"i",InternalEndpoint:"http://model-serving-d:8000/v1",ServedModelName:"deployment-d",Serving:&deploymentv1.ServingSpec{Engine:"vllm",Profile:serving.RerankerProfile,MaxModelLen:512}}
    p:=&rerankingv1.RerankerProfile{DeploymentId:"d",ModelVersionId:"v",RuntimeContainerId:"c",RuntimeImageId:"i",ServedModelName:"deployment-d",MaxInputTokens:512,ServingProfile:serving.RerankerProfile}
    if e:=rerankerReady(d);e!=nil{t.Fatal(e)}
    if e:=rerankerUnchanged(p,d);e!=nil{t.Fatal(e)}
    for _,change:=range []func(*deploymentv1.Deployment){
        func(d *deploymentv1.Deployment){d.Serving.Profile=""},func(d *deploymentv1.Deployment){d.Serving.Profile=serving.EmbeddingProfile},
        func(d *deploymentv1.Deployment){d.Serving.MaxModelLen=2048},func(d *deploymentv1.Deployment){d.ArtifactKind="adapter"},
        func(d *deploymentv1.Deployment){d.DeletedAt=timestamppb.Now()},func(d *deploymentv1.Deployment){d.DesiredState="stopped"},
        func(d *deploymentv1.Deployment){d.RuntimeState="stopping"},func(d *deploymentv1.Deployment){d.Status="failed"},
        func(d *deploymentv1.Deployment){d.RuntimeContainerId="new"},func(d *deploymentv1.Deployment){d.RuntimeImageId="new"},
        func(d *deploymentv1.Deployment){d.ModelVersionId="new"},func(d *deploymentv1.Deployment){d.InternalEndpoint="http://external"},
        func(d *deploymentv1.Deployment){d.DeploymentId="other"},func(d *deploymentv1.Deployment){d.ServedModelName="other"},
    }{changed:=proto.Clone(d).(*deploymentv1.Deployment);change(changed);if rerankerUnchanged(p,changed)==nil{t.Fatal("changed deployment accepted")}}
}
