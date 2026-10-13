package app

import (
	"context"
	"encoding/json"
	"strings"

	managementv1 "model-platform/gen/management/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/trainingevaluation"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func normalizeEvaluation(req *trainingv1.EvaluationSpec) (*trainingv1.EvaluationSpec,error) {
	if req==nil{return nil,nil}
	if req.GetDataset()==nil{return nil,status.Error(codes.InvalidArgument,"evaluation.dataset is required")}
	d,err:=NormalizeDatasetSpec(req.GetDataset());if err!=nil{return nil,status.Error(codes.InvalidArgument,err.Error())}
	if d.SourceType!="version"{return nil,status.Error(codes.InvalidArgument,"evaluation requires a registered dataset version")}
	return &trainingv1.EvaluationSpec{Dataset:d.Proto()},nil
}

func storedEvaluation(raw []byte) (*trainingv1.EvaluationSpec,error) {
	var spec *trainingv1.EvaluationSpec
	if len(raw)==0{return nil,nil}
	if text:=strings.TrimSpace(string(raw));text=="{}"||text=="null"{return nil,nil} // Legacy default.
	if err:=json.Unmarshal(raw,&spec);err!=nil{return nil,status.Error(codes.Internal,"invalid stored evaluation spec")}
	if spec==nil||spec.GetDataset()==nil{return nil,status.Error(codes.Internal,"stored evaluation dataset missing")}
	return spec,nil
}

func validateEvaluationResult(raw,runID,sourceID string,training *trainingv1.TrainingSpec,spec *trainingv1.EvaluationSpec) error {
	if spec==nil{return nil}
	r,err:=trainingevaluation.FromJSON(raw);if err==nil{err=r.Validate(runID,sourceID,training,spec)}
	if err!=nil{return status.Error(codes.FailedPrecondition,err.Error())};return nil
}

func(s *Service)GetTrainingEvaluation(ctx context.Context,req *trainingv1.GetTrainingRunRequest)(*trainingv1.TrainingEvaluation,error){
	id,err:=trainingID(req.GetTrainingRunId());if err!=nil{return nil,err}
	run,err:=s.get(ctx,id);if err!=nil{return nil,err}
	out:=&trainingv1.TrainingEvaluation{TrainingRunId:id,SourceModelVersionId:run.GetSourceModelVersionId(),Evaluation:run.GetEvaluation(),MlflowRunId:run.GetMlflowRunId(),State:"disabled"}
	if run.GetEvaluation()==nil{return out,nil}
	out.State="pending"
	raw:=run.GetResultJson();outputID:=run.GetOutputModelVersionId();origin:="training"
	if run.GetStatus()!="succeeded" {
		if run.GetOutputRecoveryStatus()=="succeeded"&&run.GetRecoveredModelVersionId()!="" {
			outputID=run.GetRecoveredModelVersionId();origin="output_recovery"
			err=s.pool.QueryRow(ctx,`SELECT result_json FROM training_output_recoveries WHERE training_run_id=$1 AND status='succeeded' AND output_model_version_id=$2 ORDER BY requested_at DESC,recovery_id DESC LIMIT 1`,id,outputID).Scan(&raw)
			if err!=nil{return nil,idempotency.DBError(err)}
		} else {if run.GetStatus()=="failed"||run.GetStatus()=="cancelled"{out.State="unavailable"};return out,nil}
	}
	result,err:=trainingevaluation.FromJSON(raw)
	if err==nil{err=result.Validate(id,run.GetSourceModelVersionId(),run.GetTraining(),run.GetEvaluation())}
	if err!=nil{return nil,status.Error(codes.FailedPrecondition,"stored evaluation evidence is invalid")}
	var evidence struct {OutputID string `json:"output_model_version_id"`;Manifest string `json:"output_manifest_sha256"`}
	if json.Unmarshal([]byte(raw),&evidence)!=nil||evidence.OutputID!=outputID||evidence.Manifest==""{return nil,status.Error(codes.FailedPrecondition,"evaluation output identity mismatch")}
	mv,err:=s.clients.Management.GetModelVersion(ctx,&managementv1.GetModelVersionRequest{ModelVersionId:outputID})
	if err!=nil{return nil,status.Error(codes.Unavailable,"evaluation model lookup unavailable")}
	if mv.GetModelVersionId()!=outputID||mv.GetState()!="ready"||mv.GetArtifactKind()!="adapter"||mv.GetTrainingRunId()!=id||mv.GetSourceModelVersionId()!=run.GetSourceModelVersionId()||mv.GetArtifactManifestSha256()!=evidence.Manifest{return nil,status.Error(codes.FailedPrecondition,"evaluation model is not the verified training output")}
	out.State="ready";out.Result=result.Proto();out.OutputModelVersionId=outputID;out.ResultOrigin=origin
	return out,nil
}

func(s *Service)CompareTrainingRuns(ctx context.Context,req *trainingv1.CompareTrainingRunsRequest)(*trainingv1.CompareTrainingRunsResponse,error){
	ids:=req.GetTrainingRunIds();if len(ids)<2||len(ids)>5{return nil,status.Error(codes.InvalidArgument,"compare requires 2..5 distinct training_run_ids")}
	seen:=map[string]bool{};normalized:=make([]string,0,len(ids))
	for _,raw:=range ids{id,err:=trainingID(strings.TrimSpace(raw));if err!=nil{return nil,err};if seen[id]{return nil,status.Error(codes.InvalidArgument,"duplicate training_run_id")};seen[id]=true;normalized=append(normalized,id)}
	out:=&trainingv1.CompareTrainingRunsResponse{}
	for _,id:=range normalized{item,err:=s.GetTrainingEvaluation(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:id});if err!=nil{return nil,err};out.Evaluations=append(out.Evaluations,item)}
	out.Comparable,out.Reason=trainingevaluation.Comparable(out.Evaluations)
	return out,nil
}
