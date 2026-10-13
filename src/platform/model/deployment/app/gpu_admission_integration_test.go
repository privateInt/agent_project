package app

import (
 "context"
 "errors"
 "testing"
 serving "model-platform/deployment/runtime"
 "model-platform/internal/gpuadmission"
)

type admissionDriver struct {
 *runtimeDriver
 busy bool
 releaseErr error
 releases int
}
func (d *admissionDriver) Create(ctx context.Context,p serving.Plan)error{
 if d.busy{return &gpuadmission.BusyError{Owner:"training-job"}}
 return d.runtimeDriver.Create(ctx,p)
}
func (d *admissionDriver) ReleaseGPU(context.Context,serving.Plan)error{d.releases++;return d.releaseErr}
func TestServingGPUWaitAndReleaseBeforeStopped(t *testing.T){
 f:=newServingFixture(t);d:=&admissionDriver{runtimeDriver:f.driver,busy:true};f.s.runtime.driver=d
 f.grant(t);f.cycle(t)
 if r:=f.view(t);r.GetRuntimeState()!="waiting_resources"||r.GetRuntimeErrorCode()!="GPU_BUSY"||r.GetApplyStatus()!="applying"{t.Fatal(r)}
 if f.driver.creates!=0||f.driver.starts!=0{t.Fatal("busy GPU launched runtime")}
 d.busy=false;f.driver.setHealthy(true);f.cycle(t);f.cycle(t)
 if f.view(t).GetStatus()!="ready"{t.Fatal("did not recover after resource release")}
 d.releaseErr=errors.New("ledger unavailable")
 if r:=f.stop(t);r.GetStatus()!="stopping"||r.GetRuntimeErrorCode()!="GPU_RELEASE_UNCONFIRMED"{t.Fatal(r)}
 d.releaseErr=nil;f.cycle(t)
 if r:=f.view(t);r.GetStatus()!="stopped"||r.GetApplyStatus()!="ready"{t.Fatal(r)}
 if d.releases<2||f.driver.starts!=1{t.Fatal("release was skipped or runtime restarted")}
}
