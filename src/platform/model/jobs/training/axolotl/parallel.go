package main

import (
 "fmt"
 "os"
 "path/filepath"
 "strconv"
 "strings"

 trainingv1 "model-platform/gen/training/v1"
)

func validateParallelRequest(run *trainingv1.TrainingRun,count int)error {
 r,t:=run.GetResources(),run.GetTraining()
 if count<1 || count>16 || r.GetGpuCount()!=int32(count) {return fmt.Errorf("Job GPU count does not match TrainingRun")}
 expected:=make([]string,0,len(r.GetGpuIds()));seen:=map[int32]bool{}
 for _,id:=range r.GetGpuIds(){if id<0 || seen[id]{return fmt.Errorf("invalid requested GPU IDs")};seen[id]=true;expected=append(expected,strconv.Itoa(int(id)))}
 if strings.Join(expected,",")!=strings.TrimSpace(os.Getenv("REQUESTED_GPU_IDS")){return fmt.Errorf("Job GPU IDs do not match TrainingRun")}
 if count>1 {
  if len(expected)!=count || t.GetStrategy()!="ddp" {return fmt.Errorf("DDP requires exact GPU IDs and strategy=ddp")}
 } else if t.GetStrategy()!="" && t.GetStrategy()!="single" {return fmt.Errorf("single GPU strategy mismatch")}
 return nil
}
func trainingCommand(config string,count int,runID string,steps int32)(string,[]string) {
 if count==1 {return "axolotl",[]string{"train",config}}
 return env("PYTHON_BINARY","python"),[]string{
  "-m","torch.distributed.run","--standalone","--nnodes=1",
  "--nproc-per-node="+strconv.Itoa(count),"--max-restarts=0",
  env("AXOLOTL_DISTRIBUTED_SCRIPT","/app/distributed_train.py"),
  "--config",config,"--evidence-dir",filepath.Join(filepath.Dir(config),"ddp-evidence"),
  "--world-size",strconv.Itoa(count),"--training-run-id",runID,"--max-steps",strconv.Itoa(int(steps)),
 }
}
