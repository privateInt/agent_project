// Package reranking owns the single-logit Model reranker contract.
package reranking

import (
    "bytes"
    "context"
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "fmt"
    "io"
    "math"
    "net/http"
    "regexp"
    "strings"
    "unicode/utf8"
    rerankingv1 "model-platform/gen/reranking/v1"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

const ProfileID = "ko-reranker-v1"
const Repository = "Dongjin-kr/ko-reranker"
const MaxCandidates = 100
const MaxInputBytes = 128 << 10
const MaxTokens = 512
const PairPolicy = "xlmr-pair-special4-no-prefix-v1"

func failure(code codes.Code, reason, message string) error { return status.Error(code, reason+": "+message) }

func Identity(p *rerankingv1.RerankerProfile) string {
    raw,_ := json.Marshal([]any{"reranker-identity-v1",p.ProfileId,p.SourceManifestSha256,p.TokenizerSha256,p.RuntimeImageId,
        p.ScoreKind,p.ScoreDirection,p.PairPolicy,p.MaxInputTokens,p.Truncation,"dtype-auto","activation-disabled"})
    sum:=sha256.Sum256(raw)
    return "rp_"+hex.EncodeToString(sum[:])
}

func ValidateRequest(r *rerankingv1.RerankRequest) error {
    bad:=func(msg string)error{return failure(codes.InvalidArgument,"RERANK_INVALID_REQUEST",msg)}
    if !regexp.MustCompile(`^rp_[a-f0-9]{64}$`).MatchString(r.GetExpectedRerankerProfileId()) {return bad("expected_reranker_profile_id is required")}
    if !utf8.ValidString(r.GetQuery()) || strings.TrimSpace(r.GetQuery())=="" {return bad("one nonempty UTF-8 query is required")}
    if len(r.GetCandidates())<1 || len(r.GetCandidates())>MaxCandidates {return bad("candidates must contain 1..100 entries")}
    seen:=map[string]bool{};size:=len(r.GetQuery())
    identifier:=regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$`)
    for _,c:=range r.GetCandidates(){
        if !identifier.MatchString(c.GetId()) || seen[c.GetId()] || !utf8.ValidString(c.GetText()) || strings.TrimSpace(c.GetText())=="" {return bad("unique candidate IDs and nonempty UTF-8 text are required")}
        seen[c.Id]=true;size+=len(c.Text)
    }
    if size>MaxInputBytes {return failure(codes.InvalidArgument,"RERANK_INPUT_TOO_LARGE","query and candidate text exceed 128 KiB")}
    return nil
}

// Reject duplicate fields anywhere in a runtime response, including score rows.
func strictJSON(raw []byte, out any) error {
    if !utf8.Valid(raw) {return fmt.Errorf("invalid UTF-8")}
    d:=json.NewDecoder(bytes.NewReader(raw));d.UseNumber()
    var walk func(int)error
    walk=func(depth int)error{
        if depth>32{return fmt.Errorf("too deep")}
        t,e:=d.Token();if e!=nil{return e}
        if delim,ok:=t.(json.Delim);ok{
            if delim=='{'{
                keys:=map[string]bool{}
                for d.More(){key,e:=d.Token();if e!=nil{return e};k,ok:=key.(string);if !ok || keys[k]{return fmt.Errorf("duplicate key")};keys[k]=true;if e=walk(depth+1);e!=nil{return e}}
            }else if delim=='['{for d.More(){if e=walk(depth+1);e!=nil{return e}}}else{return fmt.Errorf("unexpected delimiter")}
            _,e=d.Token();return e
        }
        return nil
    }
    if e:=walk(0);e!=nil{return e}
    if _,e:=d.Token();e!=io.EOF{return fmt.Errorf("trailing JSON")}
    return json.Unmarshal(raw,out)
}

func Infer(ctx context.Context, client *http.Client, origin string, p *rerankingv1.RerankerProfile, r *rerankingv1.RerankRequest)(*rerankingv1.RerankResponse,error){
    if err:=ValidateRequest(r);err!=nil{return nil,err}
    if p==nil || p.ProfileId!=ProfileID || p.MaxInputTokens!=MaxTokens || p.PairPolicy!=PairPolicy || p.ScoreKind!="logit" || p.ScoreDirection!="higher_is_better" || p.Truncation!="reject" || p.RerankerProfileId!=Identity(p){
        return nil,failure(codes.FailedPrecondition,"RERANK_UNSUPPORTED_PROFILE","invalid pinned scoring policy")
    }
    if r.ExpectedRerankerProfileId!=p.RerankerProfileId {return nil,failure(codes.FailedPrecondition,"RERANK_PROFILE_MISMATCH","resolve the intended profile before retrying")}
    invalid:=func()error{return failure(codes.DataLoss,"RERANK_INVALID_RESPONSE","runtime result could not be validated")}
    tokenize:=func(text string)(int,error){
        var result struct{Count *int `json:"count"`;MaxModelLen *int `json:"max_model_len"`;Tokens []*int `json:"tokens"`}
        if err:=post(ctx,client,origin+"/tokenize",map[string]any{"model":p.ServedModelName,"prompt":text,"add_special_tokens":false},&result,true);err!=nil{return 0,err}
        if result.Count==nil || *result.Count<1 || *result.Count!=len(result.Tokens) || result.MaxModelLen==nil || *result.MaxModelLen!=MaxTokens{return 0,invalid()}
        for _,v:=range result.Tokens{if v==nil || *v<0 || *v>=250002{return 0,invalid()}}
        return *result.Count,nil
    }
    nq,err:=tokenize(r.Query);if err!=nil{return nil,err}
    tooLong:=func()error{return failure(codes.InvalidArgument,"RERANK_INPUT_TOO_LONG","query plus candidate exceeds 512 tokens; truncation is disabled")}
    if nq+4>=MaxTokens{return nil,tooLong()}
    counts:=make([]int,len(r.Candidates));texts:=make([]string,len(r.Candidates));total:=0
    for i,c:=range r.Candidates{
        n,e:=tokenize(c.Text);if e!=nil{return nil,e}
        counts[i]=nq+n+4;if counts[i]>MaxTokens{return nil,tooLong()}
        texts[i]=c.Text;total+=counts[i]
    }
    var result struct{
        Model string `json:"model"`
        Data []struct{Index *int `json:"index"`;Score *float64 `json:"score"`} `json:"data"`
        Usage struct{Prompt *int `json:"prompt_tokens"`;Total *int `json:"total_tokens"`} `json:"usage"`
    }
    body:=map[string]any{"model":p.ServedModelName,"text_1":r.Query,"text_2":texts,"use_activation":false,"truncate_prompt_tokens":nil}
    if err=post(ctx,client,origin+"/score",body,&result,false);err!=nil{return nil,err}
    if result.Model!=p.ServedModelName || len(result.Data)!=len(r.Candidates) || result.Usage.Prompt==nil || *result.Usage.Prompt!=total || result.Usage.Total==nil || *result.Usage.Total!=total{return nil,invalid()}
    outputs:=make([]*rerankingv1.Score,len(r.Candidates))
    for _,row:=range result.Data{
        if row.Index==nil || *row.Index<0 || *row.Index>=len(outputs) || outputs[*row.Index]!=nil || row.Score==nil || math.IsNaN(*row.Score) || math.IsInf(*row.Score,0){return nil,invalid()}
        i:=*row.Index;outputs[i]=&rerankingv1.Score{Id:r.Candidates[i].Id,Index:int32(i),Score:*row.Score,InputTokens:int32(counts[i])}
    }
    return &rerankingv1.RerankResponse{Profile:p,Data:outputs},nil
}
