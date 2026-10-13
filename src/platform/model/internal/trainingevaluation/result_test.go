package trainingevaluation

import (
 "encoding/json"
 "math"
 "strings"
 "testing"
 trainingv1 "model-platform/gen/training/v1"
 "google.golang.org/protobuf/proto"
)
func evaluationFixture()(Result,*trainingv1.TrainingSpec,*trainingv1.EvaluationSpec){
 p:=math.Exp(3.5)
 r:=Result{Protocol:Protocol,CompatibilitySHA256:strings.Repeat("a",64),DatasetVersionID:"eval-version",DatasetSHA256:strings.Repeat("b",64),RecordCount:2,ScoredTokens:4,SequenceLength:256,TrainedSteps:2,NLLSum:14,Loss:3.5,Perplexity:&p,TokenizedSHA256:strings.Repeat("c",64),RunID:"run",SourceID:"base"}
 return r,&trainingv1.TrainingSpec{SequenceLength:256,MaxSteps:2},&trainingv1.EvaluationSpec{Dataset:&trainingv1.DatasetSpec{SourceType:"version",DatasetVersionId:r.DatasetVersionID,Sha256:r.DatasetSHA256,SizeBytes:200,RecordCount:2}}
}
func TestEvaluationValidatesCoverageIdentityAndWeightedMetrics(t *testing.T){
 r,training,spec:=evaluationFixture();if e:=r.Validate("run","base",training,spec);e!=nil{t.Fatal(e)}
 for name,change:=range map[string]func(*Result){
  "partial-records":func(r *Result){r.RecordCount=1},"no-tokens":func(r *Result){r.ScoredTokens=0},"too-many-tokens":func(r *Result){r.ScoredTokens=1000},
  "different-run":func(r *Result){r.RunID="other"},"different-base":func(r *Result){r.SourceID="other"},"dataset-changed":func(r *Result){r.DatasetSHA256=strings.Repeat("d",64)},
  "wrong-version":func(r *Result){r.DatasetVersionID="other"},"unfinished":func(r *Result){r.TrainedSteps=1},"wrong-sequence":func(r *Result){r.SequenceLength=128},
  "mean-of-means":func(r *Result){r.Loss=3},"nan":func(r *Result){r.NLLSum=math.NaN()},"infinity":func(r *Result){r.Loss=math.Inf(1)},
  "negative":func(r *Result){r.NLLSum=-1},"missing-perplexity":func(r *Result){r.Perplexity=nil},"wrong-perplexity":func(r *Result){p:=1.;r.Perplexity=&p},"signature":func(r *Result){r.CompatibilitySHA256=""},
 }{t.Run(name,func(t *testing.T){changed:=r;change(&changed);if e:=changed.Validate("run","base",training,spec);e==nil{t.Fatal("invalid evaluation accepted")}})}
 r.Loss=701;r.NLLSum=2804;r.Perplexity=nil;r.PerplexityOverflow=true
 if e:=r.Validate("run","base",training,spec);e!=nil{t.Fatal("valid overflow rejected",e)}
 r.PerplexityOverflow=false;if e:=r.Validate("run","base",training,spec);e==nil{t.Fatal("unmarked overflow accepted")}
}
func TestEvaluationRequiresExplicitNonNullEvidenceFields(t *testing.T){
 r,_,_:=evaluationFixture();raw,_:=json.Marshal(r);var fields map[string]any;_ = json.Unmarshal(raw,&fields)
 wrapper,_:=json.Marshal(map[string]any{"evaluation":fields});if _,e:=FromJSON(string(wrapper));e!=nil{t.Fatal(e)}
 for _,field:=range []string{"nll_sum","loss","perplexity_overflow","record_count","trained_steps"}{
  original:=fields[field]
  for _,missing:=range []bool{true,false}{if missing{delete(fields,field)}else{fields[field]=nil};raw,_:=json.Marshal(map[string]any{"evaluation":fields});if _,e:=FromJSON(string(raw));e==nil{t.Fatal("missing/null field accepted",field)}}
  fields[field]=original
 }
 for _,raw:=range []string{`{}`,`{"evaluation":null}`,`{"Evaluation":{}}`,`{"evaluation":[]}`}{if _,e:=FromJSON(raw);e==nil{t.Fatal(raw)}}
}
func TestComparisonOnlyRanksIdenticalEvaluationConditions(t *testing.T){
 r,_,_:=evaluationFixture();first:=&trainingv1.TrainingEvaluation{State:"ready",Result:r.Proto()}
 second:=proto.Clone(first).(*trainingv1.TrainingEvaluation);second.Result.TrainingRunId="another-run";second.Result.TrainedSteps=8;second.Result.Loss=2
 if ok,reason:=Comparable([]*trainingv1.TrainingEvaluation{first,second});!ok||reason!=""{t.Fatal("different training steps should be comparable",reason)}
 for name,change:=range map[string]func(*trainingv1.TrainingEvaluation){
  "pending":func(v *trainingv1.TrainingEvaluation){v.State="pending"},"missing":func(v *trainingv1.TrainingEvaluation){v.Result=nil},
  "dataset":func(v *trainingv1.TrainingEvaluation){v.Result.DatasetVersionId="other"},"base":func(v *trainingv1.TrainingEvaluation){v.Result.SourceModelVersionId="other"},
  "tokens":func(v *trainingv1.TrainingEvaluation){v.Result.TokenizedSha256="other"},"protocol":func(v *trainingv1.TrainingEvaluation){v.Result.Protocol="other"},
  "engine":func(v *trainingv1.TrainingEvaluation){v.Result.CompatibilitySha256="other"},"coverage":func(v *trainingv1.TrainingEvaluation){v.Result.ScoredTokens++},
 }{t.Run(name,func(t *testing.T){v:=proto.Clone(second).(*trainingv1.TrainingEvaluation);change(v);if ok,reason:=Comparable([]*trainingv1.TrainingEvaluation{first,v});ok||reason==""{t.Fatal("incompatible results ranked")}})}
}
