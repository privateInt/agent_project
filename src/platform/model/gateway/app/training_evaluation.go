package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/url"
	"strings"
	"time"

	trainingv1 "model-platform/gen/training/v1"
	"github.com/gofiber/fiber/v3"
)

// Validate the new nested object explicitly; do not silently ignore client pins
// or a misspelled evaluation field and then start an unevaluated training Job.
func uniqueEvaluationObject(raw []byte)(map[string]json.RawMessage,error){
 decoder:=json.NewDecoder(bytes.NewReader(raw));token,err:=decoder.Token()
 if err!=nil||token!=json.Delim('{'){return nil,fmt.Errorf("expected JSON object")}
 result:=map[string]json.RawMessage{}
 for decoder.More(){token,err:=decoder.Token();if err!=nil{return nil,err};key,ok:=token.(string);if !ok{return nil,fmt.Errorf("invalid object key")};if _,exists:=result[key];exists{return nil,fmt.Errorf("duplicate JSON field %s",key)};var value json.RawMessage;if err=decoder.Decode(&value);err!=nil{return nil,err};result[key]=value}
 if _,err=decoder.Token();err!=nil{return nil,err};if _,err=decoder.Token();err!=io.EOF{return nil,fmt.Errorf("trailing JSON content")};return result,nil
}
func parseEvaluationBody(raw []byte)(*trainingv1.EvaluationSpec,error){
	root,err:=uniqueEvaluationObject(raw);if err!=nil{return nil,err}
	for key:=range root{if strings.EqualFold(key,"evaluation")&&key!="evaluation"{return nil,fmt.Errorf("evaluation field is case-sensitive")}}
	value,ok:=root["evaluation"];if !ok||bytes.Equal(bytes.TrimSpace(value),[]byte("null")){return nil,nil}
	object,err:=uniqueEvaluationObject(value)
	if err!=nil||len(object)!=1||object["dataset"]==nil{return nil,fmt.Errorf("evaluation accepts only a dataset")}
	dataset,err:=uniqueEvaluationObject(object["dataset"])
	if err!=nil{return nil,fmt.Errorf("evaluation.dataset is required and must have unique fields")}
	for key:=range dataset{if key!="source_type"&&key!="format"&&key!="dataset_version_id"{return nil,fmt.Errorf("evaluation dataset accepts only source_type, format and dataset_version_id")}}
	var body datasetBody
	if err:=json.Unmarshal(object["dataset"],&body);err!=nil{return nil,err}
	return &trainingv1.EvaluationSpec{Dataset:body.proto()},nil
}

func evaluationSpecMap(spec *trainingv1.EvaluationSpec)any{
	if spec==nil{return nil};return fiber.Map{"dataset":datasetMap(spec.GetDataset())}
}
func evaluationMap(out *trainingv1.TrainingEvaluation)fiber.Map{
	var result any
	if r:=out.GetResult();r!=nil{result=fiber.Map{"protocol":r.GetProtocol(),"compatibility_sha256":r.GetCompatibilitySha256(),"dataset_version_id":r.GetDatasetVersionId(),"dataset_sha256":r.GetDatasetSha256(),"record_count":r.GetRecordCount(),"scored_tokens":r.GetScoredTokens(),"sequence_length":r.GetSequenceLength(),"trained_steps":r.GetTrainedSteps(),"nll_sum":r.GetNllSum(),"loss":r.GetLoss(),"perplexity":r.Perplexity,"perplexity_overflow":r.GetPerplexityOverflow(),"tokenized_sha256":r.GetTokenizedSha256(),"training_run_id":r.GetTrainingRunId(),"source_model_version_id":r.GetSourceModelVersionId()}}
	return fiber.Map{"training_run_id":out.GetTrainingRunId(),"state":out.GetState(),"evaluation":evaluationSpecMap(out.GetEvaluation()),"result":result,"output_model_version_id":out.GetOutputModelVersionId(),"source_model_version_id":out.GetSourceModelVersionId(),"mlflow_run_id":out.GetMlflowRunId(),"result_origin":out.GetResultOrigin()}
}
func(a *API)getTrainingEvaluation(c fiber.Ctx)error{
	if !emptyOutputRequest(c){return c.Status(400).JSON(fiber.Map{"error":"evaluation lookup does not accept body or query"})}
	ctx,cancel:=context.WithTimeout(c.Context(),15*time.Second);defer cancel()
	out,err:=a.clients.Training.GetTrainingEvaluation(ctx,&trainingv1.GetTrainingRunRequest{TrainingRunId:c.Params("id")});if err!=nil{return grpcError(c,err)}
	return c.JSON(evaluationMap(out))
}
func(a *API)compareTrainingRuns(c fiber.Ctx)error{
	q,err:=url.ParseQuery(string(c.Request().URI().QueryString()))
	if err!=nil||len(c.Body())!=0||len(q)!=1||len(q["training_run_ids"])!=1{return c.Status(400).JSON(fiber.Map{"error":"compare accepts only training_run_ids=id1,id2"})}
	ids:=strings.Split(q.Get("training_run_ids"),",")
	if len(ids)<2||len(ids)>5{return c.Status(400).JSON(fiber.Map{"error":"compare requires 2..5 training run IDs"})}
	ctx,cancel:=context.WithTimeout(c.Context(),25*time.Second);defer cancel()
	out,err:=a.clients.Training.CompareTrainingRuns(ctx,&trainingv1.CompareTrainingRunsRequest{TrainingRunIds:ids});if err!=nil{return grpcError(c,err)}
	items:=[]fiber.Map{}
	for _,item:=range out.GetEvaluations(){row:=evaluationMap(item);row["loss_delta_from_first"]=nil;if out.GetComparable()&&len(out.GetEvaluations())>0{row["loss_delta_from_first"]=item.GetResult().GetLoss()-out.Evaluations[0].GetResult().GetLoss()};items=append(items,row)}
	return c.JSON(fiber.Map{"comparable":out.GetComparable(),"reason":out.GetReason(),"items":items})
}
