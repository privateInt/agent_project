package app

import (
    "testing"
    serving "model-platform/deployment/runtime"
    deploymentv1 "model-platform/gen/deployment/v1"
    "google.golang.org/protobuf/proto"
    "google.golang.org/protobuf/types/known/timestamppb"
)

func TestEmbeddingReadyRejectsStoppedDeletedAndNonEmbeddingPlans(t *testing.T){
    ready:=&deploymentv1.Deployment{Status:"ready",RuntimeState:"ready",DesiredState:"running",ArtifactKind:"snapshot",Serving:&deploymentv1.ServingSpec{Engine:"vllm",MaxModelLen:2048}}
    if err:=embeddingReady(ready);err!=nil{t.Fatal("legacy candidate rejected",err)}
    ready.Serving.Profile=serving.EmbeddingProfile
    if err:=embeddingReady(ready);err!=nil{t.Fatal("explicit profile rejected",err)}
    for _,change:=range []func(*deploymentv1.Deployment){
        func(d *deploymentv1.Deployment){d.DesiredState="stopped"},func(d *deploymentv1.Deployment){d.RuntimeState="failed"},func(d *deploymentv1.Deployment){d.Status="stopped"},
        func(d *deploymentv1.Deployment){d.DeletedAt=timestamppb.Now()},func(d *deploymentv1.Deployment){d.ArtifactKind="adapter"},func(d *deploymentv1.Deployment){d.Serving.Engine="demo"},
        func(d *deploymentv1.Deployment){d.Serving.Profile=serving.SuryaProfile},func(d *deploymentv1.Deployment){d.Serving.MaxModelLen=8193},
    }{
        d:=proto.Clone(ready).(*deploymentv1.Deployment);change(d);if embeddingReady(d)==nil{t.Fatal("unsafe runtime accepted",d)}
    }
}
