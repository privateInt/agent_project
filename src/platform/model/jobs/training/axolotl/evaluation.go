package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/trainingevaluation"
)

type evaluationInput struct {
	Spec *trainingv1.EvaluationSpec `json:"spec"`
	TrainDataset *trainingv1.DatasetSpec `json:"train_dataset"`
	DatasetPath string `json:"dataset_path"`
	RunID string `json:"training_run_id"`
	SourceID string `json:"source_model_version_id"`
}

func resolveEvaluation(ctx context.Context,run *trainingv1.TrainingRun,workspace string)(*evaluationInput,error){
	if run.GetEvaluation()==nil{return nil,nil}
	spec:=run.GetEvaluation();training:=run.GetTraining()
	if run.GetResources().GetGpuCount()!=1||run.GetDataset().GetSourceType()!="version"||spec.GetDataset().GetSourceType()!="version"||training.GetSamplePacking()||training.GetSequenceLength()<2||training.GetSequenceLength()>trainingevaluation.MaxSequenceLength||spec.GetDataset().GetRecordCount()>trainingevaluation.MaxRecords{return nil,fmt.Errorf("invalid held-out evaluation settings")}
	if run.GetResources().GetNodeName()!=""&&run.GetResources().GetNodeName()!=os.Getenv("EXECUTION_NODE_NAME"){return nil,fmt.Errorf("evaluation requires the local node")}
	if run.GetDataset().GetDatasetVersionId()==spec.GetDataset().GetDatasetVersionId()||run.GetDataset().GetSha256()==spec.GetDataset().GetSha256(){return nil,fmt.Errorf("training/evaluation data must differ")}
	path,_,err:=resolveDataset(ctx,spec.GetDataset(),filepath.Join(workspace,"evaluation"));if err!=nil{return nil,err}
	return &evaluationInput{Spec:spec,TrainDataset:run.GetDataset(),DatasetPath:path,RunID:run.GetTrainingRunId(),SourceID:run.GetSourceModelVersionId()},nil
}

func readEvaluation(workspace string,run *trainingv1.TrainingRun)(*trainingevaluation.Result,error){
	raw,err:=os.ReadFile(filepath.Join(workspace,"evaluation-result.json"));if err!=nil{return nil,err}
	wrapper,err:=json.Marshal(map[string]json.RawMessage{"evaluation":raw});if err!=nil{return nil,err}
	r,err:=trainingevaluation.FromJSON(string(wrapper));if err!=nil{return nil,err}
	if err=r.Validate(run.GetTrainingRunId(),run.GetSourceModelVersionId(),run.GetTraining(),run.GetEvaluation());err!=nil{return nil,err}
	return &r,nil
}
