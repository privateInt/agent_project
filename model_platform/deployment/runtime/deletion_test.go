package runtime
import (
    "strings"
    "context"
    "encoding/json"
    "os"
    "path/filepath"
    "testing"
)
func TestRuntimeRemovalRequiresExactStoppedContainer(t *testing.T) {
    id:=strings.Repeat("b",64)
    for _,state:=range []string{"exited","created"} {
        e:=&Evidence{ID:id};e.State.Status=state
        if err:=removableRuntime(e,id);err!=nil { t.Fatal(err) }
        for _,expected:=range []string{"",id[:12],strings.Repeat("c",64)} { if err:=removableRuntime(e,expected);err==nil { t.Fatal("wrong identity accepted") } }
    }
    for _,state:=range []string{"running","paused","restarting","removing","dead",""} {
        e:=&Evidence{ID:id};e.State.Status=state
        if err:=removableRuntime(e,id);err==nil { t.Fatal("active or unknown state accepted",state) }
    }
    e:=&Evidence{ID:id};e.State.Status="exited";e.State.Running=true
    if err:=removableRuntime(e,id);err==nil { t.Fatal("inconsistent running state accepted") }
}

func TestDockerDeleteUsesValidatedIdentityAndNeverForce(t *testing.T) {
    p:=testPlan();cfg:=Config{Network:"ops-infra",Volume:"test-cache"};e:=evidence(p,cfg);e.State.Status="exited"
    dir:=t.TempDir();raw,err:=json.Marshal(e);if err!=nil { t.Fatal(err) }
    if err=os.WriteFile(filepath.Join(dir,"evidence.json"),raw,0600);err!=nil { t.Fatal(err) }
    script:=`#!/bin/sh
case "$1" in
inspect)
  if [ -f "$MODEL_DELETE_TEST_DIR/removed" ]; then echo 'No such container' >&2; exit 1; fi
  cat "$MODEL_DELETE_TEST_DIR/evidence.json" ;;
rm)
  [ "$#" -eq 2 ] || exit 2
  [ "$2" = "$MODEL_DELETE_TEST_ID" ] || exit 3
  : > "$MODEL_DELETE_TEST_DIR/removed" ;;
*) exit 4 ;;
esac
`
    if err=os.WriteFile(filepath.Join(dir,"docker"),[]byte(script),0700);err!=nil { t.Fatal(err) }
    t.Setenv("PATH",dir+string(os.PathListSeparator)+os.Getenv("PATH"));t.Setenv("MODEL_DELETE_TEST_DIR",dir);t.Setenv("MODEL_DELETE_TEST_ID",e.ID)
    driver:=Docker{Config:cfg}
    if err=driver.RemoveStopped(context.Background(),p,strings.Repeat("c",64));err==nil { t.Fatal("wrong ID removed") }
    if _,err=os.Stat(filepath.Join(dir,"removed"));!os.IsNotExist(err) { t.Fatal("unsafe removal command") }
    if err=driver.RemoveStopped(context.Background(),p,e.ID);err!=nil { t.Fatal(err) }
    if _,err=os.Stat(filepath.Join(dir,"removed"));err!=nil { t.Fatal("removal missing",err) }
    if err=driver.RemoveStopped(context.Background(),p,e.ID);err!=nil { t.Fatal("missing-container retry failed",err) }
}
