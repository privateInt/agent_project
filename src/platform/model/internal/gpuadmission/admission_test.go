package gpuadmission

import (
 "context"
 "errors"
 "os"
 "path/filepath"
 "strings"
 "sync"
 "sync/atomic"
 "testing"
 "time"
)

func req(name string,ids ...int32)Request{return Request{Name:name,OperationID:name,GPUIds:SortedIDs(ids),GPUCount:int32(len(ids)),Fenced:true}}
func active(name string,ids ...string)Container{
 c:=Container{Name:"/"+name,ID:name};c.State.Status="running";c.State.Running=true
 c.HostConfig.DeviceRequests=[]DeviceRequest{{Driver:"nvidia",DeviceIDs:ids}};return c
}
func testGuard(t *testing.T,items *[]Container)Guard{t.Helper();return Guard{Dir:t.TempDir(),Inventory:func(context.Context)([]Container,error){return *items,nil}}}
func TestUncertainCreateRetainsGPUsAcrossRestart(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background()
 lost:=errors.New("reply lost")
 if err:=g.Run(ctx,req("first",0,1),func()error{return lost});!errors.Is(err,lost){t.Fatal(err)}
 g2:=Guard{Dir:g.Dir,Inventory:g.Inventory};called:=false
 err:=g2.Run(ctx,req("second",1),func()error{called=true;return nil})
 var busy *BusyError;if !errors.As(err,&busy)||called{t.Fatalf("uncertain reservation lost: %v",err)}
 // A disjoint GPU remains usable.
 if err:=g2.Run(ctx,req("third",2),func()error{return nil});err!=nil{t.Fatal(err)}
}
func TestCreatedStoppedAndRemovedReservations(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background()
 c:=active("first","0");c.State.Running=false;c.State.Status="created"
 if err:=g.Run(ctx,req("first",0),func()error{items=[]Container{c};return nil});err!=nil{t.Fatal(err)}
 if err:=g.Run(ctx,req("second",0),func()error{return nil});err==nil{t.Fatal("created container was not reserved")}
 items[0].State.Status="exited"
 if err:=g.Run(ctx,req("second",0),func()error{items=[]Container{active("second","0")};return nil});err!=nil{t.Fatal(err)}
 // Removing a previously seen container (e.g. terminal GC) removes occupancy.
 items=nil
 if err:=g.Run(ctx,req("third",0),func()error{return nil});err!=nil{t.Fatal(err)}
}
func TestGPUAdmissionSerializesConcurrentCreators(t *testing.T){
 g:=Guard{Dir:t.TempDir(),Inventory:func(context.Context)([]Container,error){return nil,nil}}
 var launches atomic.Int32;var wg sync.WaitGroup;results:=make(chan error,2)
 for _,name:=range []string{"first","second"}{wg.Add(1);go func(name string){defer wg.Done();results<-g.Run(context.Background(),req(name,0),func()error{launches.Add(1);time.Sleep(20*time.Millisecond);return nil})}(name)}
 wg.Wait();close(results);passed,busyCount:=0,0
 for err:=range results {var busy *BusyError;if err==nil{passed++}else if errors.As(err,&busy){busyCount++}else{t.Fatal(err)}}
 if launches.Load()!=1||passed!=1||busyCount!=1{t.Fatal("same GPU admitted twice")}
}
func TestLegacyAndExternalGPUContainers(t *testing.T){
 for _,tc:=range []struct{name string;c Container;blocked bool}{
  {"exact-overlap",active("external","1"),true},
  {"exact-disjoint",active("external","2"),false},
  {"uuid-conservative",active("external","GPU-unknown"),true},
  {"count-conservative",active("external"),true},
 }{t.Run(tc.name,func(t *testing.T){items:=[]Container{tc.c};g:=testGuard(t,&items);called:=false
  err:=g.Run(context.Background(),req("new",1),func()error{called=true;return nil})
  if (err!=nil)!=tc.blocked || called==tc.blocked{t.Fatalf("wrong admission: %v",err)}
 })}
}
func TestUnknownInventoryOrCorruptStateBlocksCreate(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);called:=false
 g.Inventory=func(context.Context)([]Container,error){return nil,errors.New("daemon unavailable")}
 if g.Run(context.Background(),req("new",0),func()error{called=true;return nil})==nil || called{t.Fatal("failed open")}
 g.Inventory=func(context.Context)([]Container,error){return nil,nil}
 if err:=os.WriteFile(filepath.Join(g.Dir,"claims.json"),[]byte(`{"version":99}`),0600);err!=nil{t.Fatal(err)}
 if g.Run(context.Background(),req("new",0),func()error{called=true;return nil})==nil || called{t.Fatal("corrupt claims ignored")}
}
func TestFencedGenerationRotationAndIdentity(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background()
 first:=req("old-token",0);first.OperationID="same-op"
 if err:=g.Run(ctx,first,func()error{return errors.New("uncertain")});err==nil{t.Fatal("expected error")}
 next:=req("new-token",0);next.OperationID="same-op"
 if err:=g.Run(ctx,next,func()error{return nil});err!=nil{t.Fatal(err)}
 changed:=next;changed.GPUIds=[]int32{1}
 if err:=g.Run(ctx,changed,func()error{return nil});err==nil || !strings.Contains(err.Error(),"identity"){t.Fatal("identity change accepted")}
}
func TestCountOnlyRequestReservesAllDevices(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);legacy:=req("legacy");legacy.GPUCount=1
 if err:=g.Run(context.Background(),legacy,func()error{return nil});err!=nil{t.Fatal(err)}
 if err:=g.Run(context.Background(),req("new",7),func()error{return nil});err==nil{t.Fatal("count-only reservation not conservative")}
}
func TestLockCancellationDoesNotInvokeCreate(t *testing.T){
 g:=Guard{Dir:t.TempDir(),Inventory:func(context.Context)([]Container,error){return nil,nil}}
 entered,release,done:=make(chan struct{}),make(chan struct{}),make(chan error,1)
 go func(){done<-g.Run(context.Background(),req("first",0),func()error{close(entered);<-release;return nil})}()
 <-entered
 ctx,cancel:=context.WithTimeout(context.Background(),10*time.Millisecond);defer cancel()
 err:=g.Run(ctx,req("second",1),func()error{t.Error("expired creator launched");return nil})
 close(release);if firstErr:=<-done;firstErr!=nil{t.Fatal(firstErr)}
 if !errors.Is(err,context.DeadlineExceeded){t.Fatal(err)}
}

func TestStoppedOwnerReleasesUncertainAndNeverStartedReservations(t *testing.T){
 for _,mode:=range []string{"absent","created","exited","remove-unconfirmed","running","foreign-owner"}{t.Run(mode,func(t *testing.T){
  items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background();first:=req("first",0)
  if err:=g.Run(ctx,first,func()error{return nil});err!=nil{t.Fatal(err)}
  if mode!="absent" && mode!="foreign-owner" {c:=active("first","0");c.State.Running=mode=="running";c.State.Status=mode;if mode=="remove-unconfirmed"{c.State.Status="created"};items=[]Container{c}}
  owner:="first";if mode=="foreign-owner"{owner="wrong"}
  removals:=0
  err:=g.ReleaseStopped(ctx,"first",owner,func(_ context.Context,id string)error{removals++;if mode!="remove-unconfirmed"{items=nil};return nil})
  blocked:=mode=="running"||mode=="foreign-owner"||mode=="remove-unconfirmed"
  if (err!=nil)!=blocked{t.Fatalf("release %s: %v",mode,err)}
  if (mode=="created"||mode=="remove-unconfirmed")!=(removals==1){t.Fatal("removed a runtime that may have started")}
  err=g.Run(ctx,req("next",0),func()error{return nil})
  if (err!=nil)!=blocked{t.Fatalf("reservation after release: %v",err)}
 })}
}
func TestBlockedAdmissionPersistsObservedContainerBeforeRemoval(t *testing.T){
 items:=[]Container{};g:=testGuard(t,&items);ctx:=context.Background()
 if err:=g.Run(ctx,req("first",0),func()error{return nil});err!=nil{t.Fatal(err)}
 items=[]Container{active("first","0")}
 if err:=g.Run(ctx,req("blocked",0),func()error{return nil});err==nil{t.Fatal("overlap admitted")}
 items=nil // A now-observed container was removed before the next observation.
 if err:=g.Run(ctx,req("next",0),func()error{return nil});err!=nil{t.Fatal("observed evidence was lost on busy return",err)}
}
