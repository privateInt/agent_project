package main

import (
 "context"
 "strings"
 "testing"
 commonv1 "model-platform/gen/common/v1"
 trainingv1 "model-platform/gen/training/v1"
 "google.golang.org/protobuf/proto"
)
func TestEvaluationJobRejectsUnsupportedPlacementAndDataBeforeDownload(t *testing.T){
 run:=&trainingv1.TrainingRun{Resources:&commonv1.ExecutionResources{GpuCount:1},Training:&trainingv1.TrainingSpec{SequenceLength:256},Dataset:&trainingv1.DatasetSpec{SourceType:"version",DatasetVersionId:"train",Sha256:strings.Repeat("a",64)},Evaluation:&trainingv1.EvaluationSpec{Dataset:&trainingv1.DatasetSpec{SourceType:"version",DatasetVersionId:"eval",Sha256:strings.Repeat("b",64),RecordCount:2}}}
 for name,change:=range map[string]func(*trainingv1.TrainingRun){
  "ddp":func(r *trainingv1.TrainingRun){r.Resources.GpuCount=2},"remote":func(r *trainingv1.TrainingRun){r.Resources.NodeName="elsewhere"},
  "packing":func(r *trainingv1.TrainingRun){r.Training.SamplePacking=true},"overlong":func(r *trainingv1.TrainingRun){r.Training.SequenceLength=8192},
  "same-file":func(r *trainingv1.TrainingRun){r.Evaluation.Dataset.Sha256=r.Dataset.Sha256},"same-version":func(r *trainingv1.TrainingRun){r.Evaluation.Dataset.DatasetVersionId=r.Dataset.DatasetVersionId},
 }{t.Run(name,func(t *testing.T){r:=proto.Clone(run).(*trainingv1.TrainingRun);change(r);if _,e:=resolveEvaluation(context.Background(),r,t.TempDir());e==nil{t.Fatal("invalid evaluation accepted")}})}
 run.Evaluation=nil;if value,e:=resolveEvaluation(context.Background(),run,t.TempDir());e!=nil||value!=nil{t.Fatal("legacy training changed",e)}
}
func TestCheckpointMetadataIncludesEvaluationOnlyWhenEnabled(t *testing.T){
 run:=&trainingv1.TrainingRun{}
 if _,ok:=checkpointMetadata(run,"token","instance")["evaluation"];ok{t.Fatal("legacy checkpoint metadata changed")}
 run.Evaluation=&trainingv1.EvaluationSpec{Dataset:&trainingv1.DatasetSpec{DatasetVersionId:"pinned"}}
 if got:=checkpointMetadata(run,"token","instance")["evaluation"].(*trainingv1.EvaluationSpec);!proto.Equal(got,run.Evaluation){t.Fatal("evaluation pin missing")}
}
