package app

import (
    "context"
    "errors"
    "os"
    "path/filepath"
    "strings"
    "testing"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    serving "model-platform/deployment/runtime"
    deploymentv1 "model-platform/gen/deployment/v1"
)

type deletingDriver struct { *runtimeDriver; removals int; fail bool }
func (d *deletingDriver) RemoveStopped(ctx context.Context,p serving.Plan,expected string) error {
    e,err:=d.Inspect(ctx,p);if err!=nil { return err };if e==nil { return nil }
    if e.ID!=expected || e.State.Running { return errors.New("unsafe runtime") }
    if d.fail { return errors.New("daemon unavailable") }
    d.mu.Lock();defer d.mu.Unlock();d.evidence=nil;d.removals++;return nil
}
type deletingFetcher struct { serving.SnapshotFetcher; fail bool; purges int }
func (f *deletingFetcher) Prepare(context.Context,serving.Plan) error { return nil }
func (f *deletingFetcher) Purge(ctx context.Context,id string) error {
    f.purges++;if f.fail { return errors.New("cache busy") };return f.SnapshotFetcher.Purge(ctx,id)
}
func TestDeploymentDeleteLifecycleAndRetry(t *testing.T) {
    f:=newServingFixture(t);ctx:=context.Background();id:=f.request.DeploymentId
    driver:=&deletingDriver{runtimeDriver:f.driver};f.s.runtime.driver=driver
    cache:=&deletingFetcher{SnapshotFetcher:serving.SnapshotFetcher{Root:t.TempDir()}};f.s.runtime.fetcher=cache
    f.grant(t);f.driver.setHealthy(true);f.cycle(t);f.cycle(t)
    request:=&deploymentv1.GetDeploymentRequest{DeploymentId:id}
    if _,err:=f.s.DeleteDeployment(ctx,request);status.Code(err)!=codes.FailedPrecondition { t.Fatal("active deployment deleted",err) }
    if driver.removals!=0 || cache.purges!=0 { t.Fatal("active deployment touched") }
    f.stop(t)
    other:=uuid.NewString()
    for _,name:=range []string{id,other} { if err:=os.MkdirAll(filepath.Join(cache.Root,name),0700);err!=nil { t.Fatal(err) };if err:=os.WriteFile(filepath.Join(cache.Root,name,"weights"),[]byte("weights"),0600);err!=nil { t.Fatal(err) } }
    // A different container must not be removed, even if the DB says stopped.
    f.driver.evidence.ID=strings.Repeat("c",64)
    if _,err:=f.s.DeleteDeployment(ctx,request);status.Code(err)!=codes.FailedPrecondition || cache.purges!=0 { t.Fatal("foreign runtime removed",err) }
    f.driver.evidence.ID=strings.Repeat("b",64)
    driver.fail=true
    if _,err:=f.s.DeleteDeployment(ctx,request);status.Code(err)!=codes.FailedPrecondition || f.view(t).GetDeletedAt()!=nil { t.Fatal("failed cleanup hidden",err) }
    driver.fail=false;cache.fail=true
    if _,err:=f.s.DeleteDeployment(ctx,request);status.Code(err)!=codes.FailedPrecondition || f.view(t).GetDeletedAt()!=nil { t.Fatal("failed cache purge hidden",err) }
    cache.fail=false
    deleted,err:=f.s.DeleteDeployment(ctx,request);if err!=nil || deleted.GetDeletedAt()==nil { t.Fatal(deleted,err) }
    replay,err:=f.s.DeleteDeployment(ctx,request);if err!=nil || !replay.GetDeletedAt().AsTime().Equal(deleted.GetDeletedAt().AsTime()) || driver.removals!=1 { t.Fatal("delete replay changed",replay,err) }
    if _,err=os.Stat(filepath.Join(cache.Root,id));!os.IsNotExist(err) { t.Fatal("cache remains",err) }
    if _,err=os.Stat(filepath.Join(cache.Root,other,"weights"));err!=nil { t.Fatal("other cache removed",err) }
    page,err:=f.s.ListDeployments(ctx,&deploymentv1.ListDeploymentsRequest{});if err!=nil || len(page.GetDeployments())!=0 { t.Fatal("deleted runtime remains listed",page,err) }
    f.cycle(t);if driver.creates!=1 || driver.starts!=1 { t.Fatal("deleted deployment recreated") }
    if _,err=f.s.StartServing(ctx,f.request);err==nil { t.Fatal("deleted deployment accepted grant") }
    if _,err=f.s.BlockModelPurge(ctx,&deploymentv1.ModelPurgeFence{ModelVersionId:f.sourceID});err!=nil { t.Fatal("deleted deployment blocked later model purge",err) }
}
func TestDeploymentDeleteBeforeStartAndMissing(t *testing.T) {
    f:=newServingFixture(t);ctx:=context.Background()
    f.s.runtime.driver=&deletingDriver{runtimeDriver:f.driver}
    f.s.runtime.fetcher=serving.SnapshotFetcher{Root:t.TempDir()}
    f.stop(t)
    if _,err:=f.s.DeleteDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:f.request.DeploymentId});err!=nil { t.Fatal("never-started deployment cleanup failed",err) }
    if _,err:=f.s.DeleteDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:uuid.NewString()});status.Code(err)!=codes.NotFound { t.Fatal(err) }
    if _,err:=f.s.DeleteDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:"bad"});status.Code(err)!=codes.InvalidArgument { t.Fatal(err) }
}

func TestDeploymentDeleteSerializesWithController(t *testing.T) {
    f:=newServingFixture(t);ctx:=context.Background();f.stop(t)
    conn,err:=f.s.pool.Acquire(ctx);if err!=nil { t.Fatal(err) };defer conn.Release()
    if _,err=conn.Exec(ctx,`SELECT pg_advisory_lock(hashtextextended($1::text,0))`,"serving:"+f.request.DeploymentId);err!=nil { t.Fatal(err) }
    defer conn.Exec(ctx,`SELECT pg_advisory_unlock(hashtextextended($1::text,0))`,"serving:"+f.request.DeploymentId)
    if _,err=f.s.DeleteDeployment(ctx,&deploymentv1.GetDeploymentRequest{DeploymentId:f.request.DeploymentId});status.Code(err)!=codes.FailedPrecondition { t.Fatal("delete bypassed controller lock",err) }
}
