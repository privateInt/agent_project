package app

import (
 "encoding/json"
 "strings"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
)
func TestCheckpointSpecPreservesLegacyJSONAndRejectsInvalidModes(t *testing.T){
 legacy,e:=NormalizeTrainingSpec(nil);if e!=nil{t.Fatal(e)};raw,_:=json.Marshal(legacy)
 if strings.Contains(string(raw),"checkpoint_steps"){t.Fatal("zero checkpoint interval changed legacy fingerprints",string(raw))}
 for _,req:=range []*trainingv1.TrainingSpec{{MaxSteps:4,CheckpointSteps:-1},{MaxSteps:4,CheckpointSteps:5},{MaxSteps:4,CheckpointSteps:2,SamplePacking:true},{MaxSteps:4,CheckpointSteps:2,Strategy:"ddp"}}{
  if _,e=NormalizeTrainingSpec(req);e==nil{t.Fatal("invalid checkpoint spec accepted",req)}
 }
 spec,e:=NormalizeTrainingSpec(&trainingv1.TrainingSpec{MaxSteps:4,CheckpointSteps:2});if e!=nil||spec.Proto().GetCheckpointSteps()!=2{t.Fatal(spec,e)}
}
