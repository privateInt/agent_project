package main

import (
 "os"
 "path/filepath"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
)
func TestCheckpointMetadataUsesGrantedProcessIdentity(t *testing.T){
 t.Setenv("DISPATCH_TOKEN","untrusted-env-token");t.Setenv("EXECUTION_INSTANCE_ID","untrusted-env-instance")
 m:=checkpointMetadata(&trainingv1.TrainingRun{TrainingRunId:"run",OperationId:"op"},"granted-token","granted-instance")
 if m["dispatch_token"]!="granted-token"||m["execution_instance_id"]!="granted-instance"{t.Fatal(m)}
}
func TestCheckpointStagingCleanupOnlyTouchesOwnedCheckpointDirectories(t *testing.T){
 root:=t.TempDir();for _,name:=range []string{"checkpoint-2","keep"}{if e:=os.Mkdir(filepath.Join(root,name),0700);e!=nil{t.Fatal(e)}}
 if e:=os.WriteFile(filepath.Join(root,"adapter_model.safetensors"),[]byte("weights"),0600);e!=nil{t.Fatal(e)}
 if e:=clearCheckpointStaging(root);e!=nil{t.Fatal(e)}
 if _,e:=os.Stat(filepath.Join(root,"checkpoint-2"));!os.IsNotExist(e){t.Fatal("transient checkpoint retained")}
 for _,name:=range []string{"keep","adapter_model.safetensors"}{if _,e:=os.Stat(filepath.Join(root,name));e!=nil{t.Fatal(e)}}
 outside:=t.TempDir();if e:=os.Symlink(outside,filepath.Join(root,"checkpoint-4"));e!=nil{t.Fatal(e)}
 if e:=clearCheckpointStaging(root);e==nil{t.Fatal("symlink checkpoint accepted")}
}
