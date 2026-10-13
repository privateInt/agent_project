// Package trainingevaluation validates held-out final-answer token loss.
package trainingevaluation

import (
	"encoding/json"
	"fmt"
	"math"
	"regexp"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/datasets"
)

const Protocol = "final_assistant_nll_v1"
const MaxRecords = 2048
const MaxSequenceLength = 4096

var digestPattern = regexp.MustCompile(`^[0-9a-f]{64}$`)

type Result struct {
	Protocol string `json:"protocol"`
	CompatibilitySHA256 string `json:"compatibility_sha256"`
	DatasetVersionID string `json:"dataset_version_id"`
	DatasetSHA256 string `json:"dataset_sha256"`
	RecordCount int64 `json:"record_count"`
	ScoredTokens int64 `json:"scored_tokens"`
	SequenceLength int32 `json:"sequence_length"`
	TrainedSteps int32 `json:"trained_steps"`
	NLLSum float64 `json:"nll_sum"`
	Loss float64 `json:"loss"`
	Perplexity *float64 `json:"perplexity"`
	PerplexityOverflow bool `json:"perplexity_overflow"`
	TokenizedSHA256 string `json:"tokenized_sha256"`
	SourceID string `json:"source_model_version_id"`
	RunID string `json:"training_run_id"`
}

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v,0) }
func closeNumber(a,b float64) bool { return math.Abs(a-b)<=1e-6*math.Max(1,math.Abs(b)) }

func (r Result) Validate(runID,sourceID string,training *trainingv1.TrainingSpec,spec *trainingv1.EvaluationSpec) error {
	d:=spec.GetDataset()
	if spec==nil || d.GetSourceType()!="version" || (datasets.Info{SHA256:d.GetSha256(),SizeBytes:d.GetSizeBytes(),RecordCount:d.GetRecordCount()}).Validate()!=nil {
		return fmt.Errorf("evaluation requires a pinned dataset")
	}
	if r.Protocol!=Protocol || r.RunID!=runID || r.SourceID!=sourceID || r.DatasetVersionID!=d.GetDatasetVersionId() || r.DatasetSHA256!=d.GetSha256() ||
		r.RecordCount!=d.GetRecordCount() || r.RecordCount<2 || r.RecordCount>MaxRecords || r.SequenceLength!=training.GetSequenceLength() || r.SequenceLength<2 || r.SequenceLength>MaxSequenceLength ||
		r.TrainedSteps!=training.GetMaxSteps() || r.ScoredTokens<r.RecordCount || r.ScoredTokens>r.RecordCount*int64(r.SequenceLength-1) ||
		!digestPattern.MatchString(r.CompatibilitySHA256) || !digestPattern.MatchString(r.TokenizedSHA256) {
		return fmt.Errorf("evaluation identity, coverage or training progress mismatch")
	}
	if !finite(r.NLLSum)||r.NLLSum<0||!finite(r.Loss)||r.Loss<0||!closeNumber(r.Loss,r.NLLSum/float64(r.ScoredTokens)) {return fmt.Errorf("invalid token-weighted evaluation loss")}
	// Use a defined portable overflow boundary instead of emitting JSON Infinity.
	if r.Loss>700 {if !r.PerplexityOverflow||r.Perplexity!=nil{return fmt.Errorf("invalid perplexity overflow")}} else {
		if r.PerplexityOverflow||r.Perplexity==nil||!finite(*r.Perplexity)||!closeNumber(*r.Perplexity,math.Exp(r.Loss)){return fmt.Errorf("invalid perplexity")}
	}
	return nil
}

func FromJSON(raw string) (Result,error) {
	var body map[string]json.RawMessage
	if err:=json.Unmarshal([]byte(raw),&body);err!=nil{return Result{},err}
	var fields map[string]json.RawMessage
	if err:=json.Unmarshal(body["evaluation"],&fields);err!=nil{return Result{},fmt.Errorf("evaluation result missing: %w",err)}
	required:=[]string{"protocol","compatibility_sha256","dataset_version_id","dataset_sha256","record_count","scored_tokens","sequence_length","trained_steps","nll_sum","loss","perplexity","perplexity_overflow","tokenized_sha256","source_model_version_id","training_run_id"}
	if len(fields)!=len(required){return Result{},fmt.Errorf("evaluation result fields missing or unknown")}
	for _,key:=range required{value,ok:=fields[key];if !ok||(key!="perplexity"&&string(value)=="null"){return Result{},fmt.Errorf("evaluation result field %s missing or null",key)}}
	var result Result
	if err:=json.Unmarshal(body["evaluation"],&result);err!=nil{return Result{},err}
	return result,nil
}
func (r Result) Proto() *trainingv1.EvaluationResult {
	return &trainingv1.EvaluationResult{Protocol:r.Protocol,CompatibilitySha256:r.CompatibilitySHA256,DatasetVersionId:r.DatasetVersionID,DatasetSha256:r.DatasetSHA256,RecordCount:r.RecordCount,ScoredTokens:r.ScoredTokens,SequenceLength:r.SequenceLength,TrainedSteps:r.TrainedSteps,NllSum:r.NLLSum,Loss:r.Loss,Perplexity:r.Perplexity,PerplexityOverflow:r.PerplexityOverflow,TokenizedSha256:r.TokenizedSHA256,SourceModelVersionId:r.SourceID,TrainingRunId:r.RunID}
}
func Comparable(items []*trainingv1.TrainingEvaluation) (bool,string) {
	if len(items)<2{return false,"at_least_two_results_required"}
	for _,item:=range items{if item.GetState()!="ready"||item.GetResult()==nil{return false,"evaluation_not_ready"}}
	first:=items[0].GetResult()
	for _,item:=range items[1:] {
		r:=item.GetResult()
		if r.GetDatasetVersionId()!=first.GetDatasetVersionId()||r.GetDatasetSha256()!=first.GetDatasetSha256(){return false,"different_evaluation_dataset"}
		if r.GetSourceModelVersionId()!=first.GetSourceModelVersionId(){return false,"different_base_model"}
		if r.GetProtocol()!=first.GetProtocol()||r.GetCompatibilitySha256()!=first.GetCompatibilitySha256()||r.GetTokenizedSha256()!=first.GetTokenizedSha256()||r.GetSequenceLength()!=first.GetSequenceLength()||r.GetRecordCount()!=first.GetRecordCount()||r.GetScoredTokens()!=first.GetScoredTokens(){return false,"different_evaluation_protocol"}
	}
	return true,""
}
