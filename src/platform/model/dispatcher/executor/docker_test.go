package executor

import (
    "context"
    "os"
    "path/filepath"
    "strings"
    "testing"

    "github.com/google/uuid"
    "model-platform/internal/execution"
)

func fakeDocker(t *testing.T, label string) string {
    t.Helper()
    dir:=t.TempDir();capture:=filepath.Join(dir,"arguments")
    script:=`#!/bin/sh
if [ "$1" = image ]; then
    case "$*" in *training-evaluation*) if [ "$GATE_TEST_EVALUATION_MISSING" = 1 ]; then printf "0\n"; exit 0; fi ;; esac
    case "$*" in *training-checkpoints*) if [ "$GATE_TEST_CHECKPOINT_MISSING" = 1 ]; then printf "0\n"; exit 0; fi ;; esac
    printf '%s\n' "$GATE_TEST_IMAGE_LABEL"
    exit 0
fi
if [ "$1" = ps ]; then
    exit 0
fi
printf '%s\n' "$@" > "$GATE_TEST_DOCKER_ARGS"
printf '%s\n' fake-container-id
`
    if err:=os.WriteFile(filepath.Join(dir,"docker"),[]byte(script),0700);err!=nil{t.Fatal(err)}
    t.Setenv("PATH",dir+string(os.PathListSeparator)+os.Getenv("PATH"))
    t.Setenv("GATE_TEST_DOCKER_ARGS",capture);t.Setenv("GATE_TEST_IMAGE_LABEL",label)
    return capture
}
func TestDockerLaunchCarriesFence(t *testing.T){
    capture:=fakeDocker(t,"1")
    cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"deployment.apply",ResourceID:uuid.NewString(),ResourceType:"deployment"}
    if err:=NewDocker("ops-infra","").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});err!=nil{t.Fatal(err)}
    data,err:=os.ReadFile(capture);if err!=nil{t.Fatal(err)}
    args:="\n"+string(data)
    for _,want:=range []string{"model-op-"+cmd.OperationID+"-"+cmd.DispatchToken,"DISPATCH_TOKEN="+cmd.DispatchToken,"model-platform.dispatch_token="+cmd.DispatchToken}{
        if !strings.Contains(args,"\n"+want+"\n"){t.Fatalf("missing %s",want)}
    }
    if strings.Contains(args,"\n--rm\n")||strings.Contains(args,"\n--restart\n"){t.Fatal("job may erase evidence or restart automatically")}
}
func TestDockerRejectsUngatedImage(t *testing.T){
    capture:=fakeDocker(t,"")
    cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString()}
    if err:=NewDocker("ops-infra","").Run(context.Background(),cmd,execution.Profile{Image:"old-image"});err==nil{t.Fatal("ungated image launched")}
    if _,err:=os.Stat(capture);!os.IsNotExist(err){t.Fatal("docker run reached for ungated image")}
}

func TestServingCoordinatorDoesNotReserveRuntimeGPUs(t *testing.T){
 capture:=fakeDocker(t,"1")
 cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"deployment.serve",Resources:execution.Resources{GPUCount:1,GPUIds:[]int32{0}}}
 if err:=NewDocker("ops-infra","").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});err!=nil{t.Fatal(err)}
 raw,err:=os.ReadFile(capture);if err!=nil{t.Fatal(err)}
 if strings.Contains(string(raw),"--gpus\n"){t.Fatal("control-only coordinator received GPU device")}
}
func TestTrainingLaunchUsesSharedAdmission(t *testing.T){
 capture:=fakeDocker(t,"1");t.Setenv("GPU_ADMISSION_DIR",t.TempDir())
 cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"training.run",Resources:execution.Resources{GPUCount:2,GPUIds:[]int32{1,3}}}
 if err:=NewDocker("ops-infra","").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});err!=nil{t.Fatal(err)}
 raw,err:=os.ReadFile(capture);if err!=nil{t.Fatal(err)}
 for _,want:=range []string{"--gpus\n\"device=1,3\"\n","--shm-size\n2g\n"}{if !strings.Contains(string(raw),want){t.Fatalf("missing launch option %q",want)}}
 if err:=os.Remove(capture);err!=nil{t.Fatal(err)}
 cmd.OperationID=uuid.NewString();cmd.DispatchToken=uuid.NewString()
 if err:=NewDocker("ops-infra","").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});err==nil{t.Fatal("uncertain launch released its GPUs")}
 if _,err:=os.Stat(capture);!os.IsNotExist(err){t.Fatal("second overlapping Job reached Docker")}
}

func TestTrainingOutputMountIsExplicitAndDoesNotAffectOtherJobs(t *testing.T){
 capture:=fakeDocker(t,"1");t.Setenv("TRAINING_OUTPUT_VOLUME","model-training-outputs")
 cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"training.run",ResourceID:uuid.NewString()}
 if e:=NewDocker("ops-infra","node-a").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});e!=nil{t.Fatal(e)}
 raw,e:=os.ReadFile(capture);if e!=nil{t.Fatal(e)}
 for _,want:=range []string{"type=volume,src=model-training-outputs,dst=/training-outputs","TRAINING_OUTPUT_ROOT=/training-outputs","EXECUTION_NODE_NAME=node-a"}{if !strings.Contains(string(raw),want){t.Fatal("missing durable output option",want)}}
 cmd.ExecutionType="deployment.apply"
 if e:=NewDocker("ops-infra","node-a").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});e!=nil{t.Fatal(e)}
 raw,e=os.ReadFile(capture);if e!=nil{t.Fatal(e)};if strings.Contains(string(raw),"training-outputs"){t.Fatal("unrelated job mounted training outputs")}
 t.Setenv("TRAINING_OUTPUT_VOLUME","bad,volume");cmd.ExecutionType="training.run"
 if e:=NewDocker("ops-infra","node-a").Run(context.Background(),cmd,execution.Profile{Image:"test-image"});e==nil{t.Fatal("invalid mount accepted")}
}

func TestCheckpointImageCapabilityIsRequiredBeforeContainerLaunch(t *testing.T){
 capture:=fakeDocker(t,"1");t.Setenv("TRAINING_OUTPUT_VOLUME","model-training-outputs");t.Setenv("GATE_TEST_CHECKPOINT_MISSING","1")
 cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"training.run",ResourceID:uuid.NewString()}
 if e:=NewDocker("ops-infra","node-a").Run(context.Background(),cmd,execution.Profile{Image:"old-training-image"});e==nil{t.Fatal("checkpoint-incompatible image launched")}
 if _,e:=os.Stat(capture);!os.IsNotExist(e){t.Fatal("rejected image reached container creation")}
}

func TestEvaluationImageCapabilityIsRequiredBeforeContainerLaunch(t *testing.T){
 capture:=fakeDocker(t,"1");t.Setenv("TRAINING_OUTPUT_VOLUME","model-training-outputs");t.Setenv("GATE_TEST_EVALUATION_MISSING","1")
 cmd:=execution.Command{OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),ExecutionType:"training.run",ResourceID:uuid.NewString()}
 if e:=NewDocker("ops-infra","node-a").Run(context.Background(),cmd,execution.Profile{Image:"old-training-image"});e==nil{t.Fatal("evaluation-incompatible image launched")}
 if _,e:=os.Stat(capture);!os.IsNotExist(e){t.Fatal("rejected image reached container creation")}
}
