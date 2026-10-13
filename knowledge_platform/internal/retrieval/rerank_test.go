package retrieval

import (
	"encoding/json"
	"math"
	"strings"
	"testing"

	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	"knowledge-platform/internal/testsupport"
	"google.golang.org/protobuf/proto"
)

func rerankBody()map[string]any{
	b:=queryBody();p:=testsupport.RerankerProfile()
	b["query_text"]=" 원문 질문 ";b["top_k"]=1;b["candidate_k"]=2
	b["rerank"]=map[string]any{"enabled":true,"deployment_id":p.DeploymentId,"expected_reranker_profile_id":p.RerankerProfileId}
	return b
}
func TestRerankRequestContract(t *testing.T){
	raw,_:=json.Marshal(rerankBody());q,e:=Parse(raw);if e!=nil||q.QueryText!=" 원문 질문 "||!q.Rerank.Enabled{t.Fatal(q,e)}
	for name,change:=range map[string]func(map[string]any){
		"no question":func(b map[string]any){delete(b,"query_text")},
		"blank":func(b map[string]any){b["query_text"]=" \n"},
		"NUL":func(b map[string]any){b["query_text"]="x\x00"},
		"bytes":func(b map[string]any){b["query_text"]=strings.Repeat("한",2800)},
		"no candidate count":func(b map[string]any){delete(b,"candidate_k")},
		"no profile":func(b map[string]any){delete(b["rerank"].(map[string]any),"expected_reranker_profile_id")},
		"wrong profile":func(b map[string]any){b["rerank"].(map[string]any)["expected_reranker_profile_id"]="latest"},
		"wrong deployment":func(b map[string]any){b["rerank"].(map[string]any)["deployment_id"]="http://model"},
		"fallback":func(b map[string]any){b["rerank"].(map[string]any)["on_failure"]="vector"},
		"missing enabled":func(b map[string]any){delete(b["rerank"].(map[string]any),"enabled")},
	}{t.Run(name,func(t *testing.T){b:=rerankBody();change(b);raw,_:=json.Marshal(b);if _,e:=Parse(raw);e==nil{t.Fatal("invalid rerank accepted")}})}
}

func TestRerankOrderingPreservesProvenanceAndRejectsBadReceipts(t *testing.T){
	v:=testsupport.SearchVersion();p:=testsupport.RerankerProfile();raw,_:=json.Marshal(rerankBody());q,_:=Parse(raw)
	a,b:=testsupport.SearchChunk(v,0),testsupport.SearchChunk(v,1)
	candidates,_:=OrderCandidates(q,Candidates{Hits:[]Hit{{Chunk:b,Score:.2},{Chunk:a,Score:.8}}})
	req,e:=RerankRequest(q,candidates,p);if e!=nil||req.Query!=q.QueryText||req.Candidates[0].Id!=a.ID||req.Candidates[1].Text!=b.Text{t.Fatal(req,e)}
	receipt:=&rerankingv1.RerankResponse{Profile:proto.Clone(p).(*RerankerProfile),Data:[]*rerankingv1.Score{{Id:a.ID,Index:0,Score:-1,InputTokens:18},{Id:b.ID,Index:1,Score:9,InputTokens:22}}}
	out,e:=FinishReranked(v.IndexID,q,candidates,p,receipt,Timings{});if e!=nil||out.Results[0].ID!=b.ID||out.Results[0].RetrievalRank!=2||out.Results[0].Score!=.2||out.Results[0].Rank!=1||out.Results[0].Text!=b.Text||string(out.Results[0].Quality)!=string(b.Quality)||!out.Rerank.Applied||out.ReturnedCount!=1||out.CandidateCount!=2{t.Fatal(out,e)}
	receipt.Data[0].Score=9;out,e=FinishReranked(v.IndexID,q,candidates,p,receipt,Timings{});if e!=nil||out.Results[0].ID!=a.ID{t.Fatal("tie did not retain retrieval rank",e)}
	for name,change:=range map[string]func(*rerankingv1.RerankResponse){
		"missing":func(r *rerankingv1.RerankResponse){r.Data=r.Data[:1]},
		"duplicate":func(r *rerankingv1.RerankResponse){r.Data[1].Id=r.Data[0].Id},
		"position":func(r *rerankingv1.RerankResponse){r.Data[0].Index=1},
		"nil":func(r *rerankingv1.RerankResponse){r.Data[0]=nil},
		"NaN":func(r *rerankingv1.RerankResponse){r.Data[0].Score=math.NaN()},
		"Inf":func(r *rerankingv1.RerankResponse){r.Data[0].Score=math.Inf(1)},
		"overlong":func(r *rerankingv1.RerankResponse){r.Data[0].InputTokens=513},
		"container changed":func(r *rerankingv1.RerankResponse){r.Profile.RuntimeContainerId=strings.Repeat("f",64)},
		"profile changed":func(r *rerankingv1.RerankResponse){r.Profile.RerankerProfileId="rp_"+strings.Repeat("f",64)},
	}{t.Run(name,func(t *testing.T){bad:=proto.Clone(receipt).(*rerankingv1.RerankResponse);change(bad);if _,e:=FinishReranked(v.IndexID,q,candidates,p,bad,Timings{});e==nil{t.Fatal("invalid receipt accepted")}})}
	if _,e:=Finish(v.IndexID,q,candidates,Timings{});e==nil{t.Fatal("enabled request silently fell back to vector")}
}

func TestRerankEmptyAndLimits(t *testing.T){
	p:=testsupport.RerankerProfile();raw,_:=json.Marshal(rerankBody());q,_:=Parse(raw)
	out,e:=FinishReranked("index",q,Candidates{},p,&rerankingv1.RerankResponse{Profile:p},Timings{})
	if e!=nil||out.Rerank.Status!="skipped_no_candidates"||out.Rerank.Applied||!out.Rerank.Requested||out.EmptyReason!="no_matches"||out.Results==nil{t.Fatal(out,e)}
	p.MaxCandidates=1;if _,e:=RerankRequest(q,Candidates{},p);e==nil{t.Fatal("profile count exceeded")};p.MaxCandidates=100
	q.QueryText=strings.Repeat("a",131073);if _,e:=RerankRequest(q,Candidates{},p);e==nil{t.Fatal("byte limit ignored")}
	p.ScoreDirection="lower_is_better";if e:=ValidateReranker(p,q.Rerank);e==nil{t.Fatal("incompatible scoring accepted")}
}
