package app

import (
 "encoding/json"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
)
func TestSingleGPUTrainingKeepsLegacyFingerprintShape(t *testing.T){
 const want=`{"method":"qlora","sequence_length":256,"max_steps":1,"micro_batch_size":1,"gradient_accumulation_steps":1,"learning_rate":0.0002,"lora":{"r":8,"alpha":16,"dropout":0},"sample_packing":false}`
 for _,req:=range []*trainingv1.TrainingSpec{nil,{Strategy:"single"},{Strategy:" SINGLE "}} {
  spec,err:=NormalizeTrainingSpec(req);if err!=nil{t.Fatal(err)}
  raw,err:=json.Marshal(spec);if err!=nil||string(raw)!=want{t.Fatalf("legacy fingerprint changed: %s %v",raw,err)}
 }
}
