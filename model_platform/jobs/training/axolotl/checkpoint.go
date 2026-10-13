package main

import (
 "context"
 "encoding/json"
 "fmt"
 "os"
 "path/filepath"
 "regexp"

 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/trainingcheckpoint"
 "model-platform/internal/trainingoutput"
 "google.golang.org/protobuf/proto"
)

func checkpointInput(ctx context.Context,run *trainingv1.TrainingRun,training trainingv1.TrainingServiceClient,operations operationv1.OperationServiceClient)(string,error){
 if run.GetTraining().GetCheckpointSteps()==0 {if run.GetResume()!=nil{return "",fmt.Errorf("resume requires checkpoint-enabled training")};return "",nil}
 if run.GetResources().GetGpuCount()!=1||os.Getenv("TRAINING_OUTPUT_ROOT")==""||run.GetTraining().GetSamplePacking(){return "",fmt.Errorf("checkpoint training requires local preserved single-GPU output")}
 if ds:=run.GetDataset().GetSourceType();ds!="synthetic"&&ds!="version"{return "",fmt.Errorf("checkpoint dataset must be immutable")}
 resume:=run.GetResume();if resume==nil{return "",nil}
 parent,e:=training.GetTrainingRun(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:resume.GetSourceTrainingRunId()});if e!=nil{return "",e}
 op,e:=operations.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:parent.GetOperationId()});if e!=nil{return "",e}
 if (parent.GetStatus()!="failed"&&parent.GetStatus()!="cancelled")||op.GetStatus()!=parent.GetStatus()||op.GetResourceId()!=parent.GetTrainingRunId()||op.GetResourceType()!="training_run"||op.GetExecutionType()!="training.run"||op.GetExecutionStartedAt()==nil{return "",fmt.Errorf("original checkpoint execution is not terminal")}
 if parent.GetSourceModelVersionId()!=run.GetSourceModelVersionId()||!proto.Equal(parent.GetTraining(),run.GetTraining())||!proto.Equal(parent.GetDataset(),run.GetDataset())||!proto.Equal(parent.GetEvaluation(),run.GetEvaluation())||!proto.Equal(parent.GetResources(),run.GetResources())||!proto.Equal(parent.GetResources(),op.GetResources()){return "",fmt.Errorf("resume changed original training settings")}
 dir,e:=trainingoutput.Directory(os.Getenv("TRAINING_OUTPUT_ROOT"),parent.GetTrainingRunId(),op.GetDispatchToken());if e!=nil{return "",e}
 r,digest,e:=trainingcheckpoint.Read(dir,resume.GetCheckpointStep());if e!=nil{return "",e}
 var evaluation *trainingv1.EvaluationSpec
 if len(r.Evaluation)>0{if e=json.Unmarshal(r.Evaluation,&evaluation);e!=nil{return "",e}}
 if !proto.Equal(evaluation,run.GetEvaluation()){return "",fmt.Errorf("checkpoint evaluation pin changed")}
 if digest!=resume.GetCheckpointSha256()||r.RunID!=parent.GetTrainingRunId()||r.SourceID!=run.GetSourceModelVersionId()||r.OperationID!=op.GetOperationId()||r.DispatchToken!=op.GetDispatchToken()||r.InstanceID!=op.GetExecutionInstanceId()||r.Node!=os.Getenv("EXECUTION_NODE_NAME")||r.Step>=r.MaxSteps{return "",fmt.Errorf("checkpoint provenance changed")}
 return filepath.Join(dir,"checkpoints",fmt.Sprintf("checkpoint-%d",r.Step)),nil
}
func checkpointMetadata(run *trainingv1.TrainingRun,token,instance string)map[string]any{
 m:=map[string]any{"training_run_id":run.GetTrainingRunId(),"operation_id":run.GetOperationId(),"dispatch_token":token,"execution_instance_id":instance,"execution_node":os.Getenv("EXECUTION_NODE_NAME"),"source_model_version_id":run.GetSourceModelVersionId(),"training":run.GetTraining(),"dataset":run.GetDataset()}
 if run.GetEvaluation()!=nil{m["evaluation"]=run.GetEvaluation()};return m
}
func readCheckpointEvidence(workspace string,run *trainingv1.TrainingRun)(trainingcheckpoint.Evidence,error){
 var e trainingcheckpoint.Evidence
 raw,err:=os.ReadFile(filepath.Join(workspace,"checkpoint-evidence.json"));if err!=nil{return e,err};if err=json.Unmarshal(raw,&e);err!=nil{return e,err}
 return e,trainingcheckpoint.ValidateEvidence(e,run.GetTrainingRunId(),run.GetTraining().GetMaxSteps(),trainingcheckpoint.FromProto(run.GetResume()))
}

// Once the audited trainer exits successfully, independent sealed copies retain
// resume state. Keep transient Trainer checkpoints out of the final model artifact.
func clearCheckpointStaging(output string)error{
 entries,e:=os.ReadDir(output);if e!=nil{return e}
 for _,entry:=range entries{
  if !checkpointStagingName.MatchString(entry.Name()){continue}
  path:=filepath.Join(output,entry.Name());if e=trainingcheckpoint.RealDirectory(path);e!=nil{return e}
  if e=os.RemoveAll(path);e!=nil{return e}
 };return nil
}
var checkpointStagingName=regexp.MustCompile(`^checkpoint-[1-9][0-9]*$`)
