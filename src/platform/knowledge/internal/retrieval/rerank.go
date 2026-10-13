package retrieval

import (
	"math"
	"sort"
	"strings"

	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	"knowledge-platform/internal/chunking"
	"google.golang.org/protobuf/proto"
)

// Use the Model contract; Knowledge never loads weights or tokenizes pairs.
type RerankerProfile = rerankingv1.RerankerProfile

func ValidRerankerID(id string) bool {
	return strings.HasPrefix(id, "rp_") && chunking.ValidDigest(strings.TrimPrefix(id, "rp_"))
}

func ValidateReranker(p *RerankerProfile, options RerankOptions) error {
	if p == nil || p.DeploymentId != options.DeploymentID || !ValidID(p.ModelVersionId) || !ValidRerankerID(p.RerankerProfileId) || p.Capability != "reranker" || p.Status != "ready" || p.ScoreKind != "logit" || p.ScoreDirection != "higher_is_better" || p.Truncation != "reject" || p.PairPolicy == "" || p.ProfileId == "" || p.ServingProfile != p.ProfileId || p.MaxInputTokens < 1 || p.MaxInputTokens > 32768 || p.MaxCandidates < 1 || p.MaxCandidates > 100 || p.MaxInputBytes < 1 || p.MaxInputBytes > 128<<10 || !chunking.ValidDigest(p.SourceManifestSha256) || !chunking.ValidDigest(p.TokenizerSha256) || !strings.HasPrefix(p.RuntimeImageId, "sha256:") || !chunking.ValidDigest(strings.TrimPrefix(p.RuntimeImageId,"sha256:")) || !chunking.ValidDigest(p.RuntimeContainerId) || p.ServedModelName != "deployment-"+p.DeploymentId {
		return invalid("SEARCH_RERANK_PROFILE_INVALID", "Model reranker profile is unsupported, incomplete or not ready")
	}
	if p.RerankerProfileId != options.ExpectedProfileID {
		return invalid("RERANK_PROFILE_MISMATCH", "current reranker profile differs from expected identity")
	}
	return nil
}

// Stable vector order is also the exact input order of the Model request.
func OrderCandidates(q Query, candidates Candidates) (Candidates, error) {
	if len(candidates.Hits) > q.CandidateK { return Candidates{}, invalid("SEARCH_RESPONSE_INVALID", "excessive vector candidates") }
	ordered := candidates
	ordered.Hits = append([]Hit{}, candidates.Hits...)
	seen := map[string]bool{}
	for _, h := range ordered.Hits {
		if !chunking.ValidDigest(h.ID) || seen[h.ID] || strings.TrimSpace(h.Text) == "" || math.IsNaN(h.Score) || math.IsInf(h.Score,0) || h.Score < 0 || h.Score > 1.000001 {
			return Candidates{}, invalid("SEARCH_RESPONSE_INVALID", "invalid vector candidates")
		}
		seen[h.ID] = true
	}
	sort.Slice(ordered.Hits, func(i,j int) bool {
		if ordered.Hits[i].Score == ordered.Hits[j].Score { return ordered.Hits[i].ID < ordered.Hits[j].ID }
		return ordered.Hits[i].Score > ordered.Hits[j].Score
	})
	for i := range ordered.Hits { ordered.Hits[i].Rank=i+1; ordered.Hits[i].RetrievalRank=i+1 }
	return ordered, nil
}

func RerankRequest(q Query, candidates Candidates, p *RerankerProfile) (*rerankingv1.RerankRequest,error) {
	if err:=ValidateReranker(p,q.Rerank);err!=nil{return nil,err}
	if q.CandidateK > int(p.MaxCandidates) { return nil,invalid("SEARCH_INVALID_REQUEST","candidate_k exceeds the selected Model limit") }
	request:=&rerankingv1.RerankRequest{DeploymentId:q.Rerank.DeploymentID,ExpectedRerankerProfileId:q.Rerank.ExpectedProfileID,Query:q.QueryText}
	size:=len(q.QueryText)
	for _,hit:=range candidates.Hits { size+=len(hit.Text);request.Candidates=append(request.Candidates,&rerankingv1.Candidate{Id:hit.ID,Text:hit.Text}) }
	if size>int(p.MaxInputBytes) { return nil,invalid("RERANK_INPUT_TOO_LARGE","query and candidates exceed the Model byte limit") }
	return request,nil
}

func FinishReranked(indexID string,q Query,candidates Candidates,p *RerankerProfile,receipt *rerankingv1.RerankResponse,timings Timings)(Response,error){
	if err:=ValidateReranker(p,q.Rerank);err!=nil{return Response{},err}
	ordered,err:=OrderCandidates(q,candidates);if err!=nil{return Response{},err}
	if receipt==nil||!proto.Equal(receipt.Profile,p)||len(receipt.Data)!=len(ordered.Hits){return Response{},invalid("SEARCH_RERANK_RESPONSE_INVALID","reranker identity or candidate count differs")}
	vectorQuery:=q;vectorQuery.Rerank.Enabled=false;vectorQuery.TopK=q.CandidateK
	out,err:=Finish(indexID,vectorQuery,ordered,timings);if err!=nil{return Response{},err}
	out.TopK=q.TopK
	out.Rerank=Rerank{Requested:true,Status:"skipped_no_candidates",DeploymentID:p.DeploymentId,ProfileID:p.RerankerProfileId,Profile:proto.Clone(p).(*RerankerProfile)}
	for i,row:=range receipt.Data {
		if row==nil||row.Id!=out.Results[i].ID||row.Index!=int32(i)||math.IsNaN(row.Score)||math.IsInf(row.Score,0)||row.InputTokens<1||row.InputTokens>p.MaxInputTokens{return Response{},invalid("SEARCH_RERANK_RESPONSE_INVALID","reranker ID, position, score or token receipt differs")}
		score:=row.Score;out.Results[i].RerankScore=&score;out.Results[i].RerankInputTokens=row.InputTokens
	}
	if len(out.Results)>0 {
		out.Rerank.Applied=true;out.Rerank.Status="applied"
		sort.Slice(out.Results,func(i,j int)bool { a,b:=out.Results[i],out.Results[j];if *a.RerankScore==*b.RerankScore{return a.RetrievalRank<b.RetrievalRank};return *a.RerankScore>*b.RerankScore })
		out.Results=out.Results[:min(q.TopK,len(out.Results))]
		for i:=range out.Results { out.Results[i].Rank=i+1 }
	}
	out.ReturnedCount=len(out.Results)
	return out,nil
}
