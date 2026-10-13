package main

import (
 "strings"
 "testing"
 commonv1 "model-platform/gen/common/v1"
 trainingv1 "model-platform/gen/training/v1"
)
func TestDDPUsesLocalRanksAndNoAutomaticTrainingRestart(t *testing.T){
 binary,args:=trainingCommand("/tmp/job/axolotl.yml",2,"run",1)
 joined:=strings.Join(args," ")
 if binary=="axolotl"||!strings.Contains(joined,"--nproc-per-node=2")||!strings.Contains(joined,"--max-restarts=0")||!strings.Contains(joined,"--standalone")||!strings.Contains(joined,"--nnodes=1"){t.Fatal(joined)}
 if strings.Contains(joined,"CUDA_VISIBLE_DEVICES"){t.Fatal("host IDs must not become container-local ranks")}
 binary,args=trainingCommand("/tmp/job/axolotl.yml",1,"run",1)
 if binary!="axolotl"||strings.Join(args," ")!="train /tmp/job/axolotl.yml"{t.Fatal("single-GPU command changed")}
}
func TestJobRejectsChangedGPUAssignment(t *testing.T){
 run:=&trainingv1.TrainingRun{Resources:&commonv1.ExecutionResources{GpuCount:2,GpuIds:[]int32{1,3}},Training:&trainingv1.TrainingSpec{Strategy:"ddp"}}
 t.Setenv("REQUESTED_GPU_IDS","1,3")
 if err:=validateParallelRequest(run,2);err!=nil{t.Fatal(err)}
 t.Setenv("REQUESTED_GPU_IDS","0,1")
 if validateParallelRequest(run,2)==nil{t.Fatal("changed physical assignment accepted")}
 t.Setenv("REQUESTED_GPU_IDS","1,3")
 if validateParallelRequest(run,1)==nil{t.Fatal("single-GPU fallback accepted")}
 run.Training.Strategy=""
 if validateParallelRequest(run,2)==nil{t.Fatal("old Job contract accepted for DDP")}
}
