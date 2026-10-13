package reranking

import (
    "context"
    "encoding/json"
    "fmt"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
    rerankingv1 "model-platform/gen/reranking/v1"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/proto"
)

func testProfile()*rerankingv1.RerankerProfile{
    p:=&rerankingv1.RerankerProfile{ProfileId:ProfileID,SourceManifestSha256:strings.Repeat("a",64),TokenizerSha256:strings.Repeat("b",64),
        RuntimeImageId:"sha256:"+strings.Repeat("c",64),ServedModelName:"owned-model",MaxInputTokens:MaxTokens,PairPolicy:PairPolicy,
        ScoreKind:"logit",ScoreDirection:"higher_is_better",Truncation:"reject"}
    p.RerankerProfileId=Identity(p);return p
}
func testRequest(p *rerankingv1.RerankerProfile)*rerankingv1.RerankRequest{return &rerankingv1.RerankRequest{
    ExpectedRerankerProfileId:p.RerankerProfileId,Query:" 질문 ",Candidates:[]*rerankingv1.Candidate{{Id:"a",Text:"first"},{Id:"b",Text:"second"}}}}

func TestRerankerIdentityAndRequestLimits(t *testing.T){
    p:=testProfile();r:=testRequest(p)
    if e:=ValidateRequest(r);e!=nil{t.Fatal(e)}
    q:=proto.Clone(p).(*rerankingv1.RerankerProfile);q.DeploymentId="other";q.RuntimeContainerId="other"
    if Identity(q)!=p.RerankerProfileId{t.Fatal("ephemeral identity affects score identity")}
    q.ScoreKind="sigmoid";if Identity(q)==p.RerankerProfileId{t.Fatal("score semantics omitted")}
    for _,change:=range []func(*rerankingv1.RerankRequest){
        func(r *rerankingv1.RerankRequest){r.Query=" "},func(r *rerankingv1.RerankRequest){r.Candidates=nil},
        func(r *rerankingv1.RerankRequest){r.Candidates[1].Id="a"},func(r *rerankingv1.RerankRequest){r.Candidates[0]=nil},
        func(r *rerankingv1.RerankRequest){r.Candidates[0].Text=string([]byte{0xff})},func(r *rerankingv1.RerankRequest){r.Query=strings.Repeat("x",MaxInputBytes)},
        func(r *rerankingv1.RerankRequest){r.ExpectedRerankerProfileId="es_"+strings.Repeat("a",64)},
        func(r *rerankingv1.RerankRequest){for i:=2;i<=MaxCandidates;i++{r.Candidates=append(r.Candidates,&rerankingv1.Candidate{Id:fmt.Sprint(i),Text:"x"})}},
    }{bad:=proto.Clone(r).(*rerankingv1.RerankRequest);change(bad);if ValidateRequest(bad)==nil{t.Fatal("invalid request accepted")}}
}

func TestRerankerInferencePreservesPairsOrderAndRejectsUnconfirmedScores(t *testing.T){
    p:=testProfile();r:=testRequest(p)
    for _,mode:=range []string{"ok","wrong-space","long-query","long-document","bad-token-count","null-token","bad-token-limit","missing","duplicate-index","null-score","array-score","duplicate-field","wrong-model","wrong-usage","oversize","http-failure","cancelled"}{t.Run(mode,func(t *testing.T){
        calls,scoring:=0,0
        server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,req *http.Request){
            calls++;var body map[string]any;if json.NewDecoder(req.Body).Decode(&body)!=nil{t.Fatal("bad request")}
            if req.URL.Path=="/tokenize"{
                if body["add_special_tokens"]!=false || body["model"]!="owned-model"{t.Error("tokenizer policy")}
                if calls==1 && body["prompt"]!=r.Query{t.Error("query was normalized")}
                tokens:=[]any{4,5};count,limit:=2,512
                if (mode=="long-query" && calls==1)||(mode=="long-document" && calls==2){count=509;tokens=make([]any,count);for i:=range tokens{tokens[i]=4}}
                if mode=="bad-token-count"{count=1};if mode=="null-token"{tokens[0]=nil};if mode=="bad-token-limit"{limit=2048}
                json.NewEncoder(w).Encode(map[string]any{"count":count,"max_model_len":limit,"tokens":tokens});return
            }
            scoring++;if req.URL.Path!="/score" || calls!=4 || body["text_1"]!=r.Query || body["use_activation"]!=false || body["truncate_prompt_tokens"]!=nil{t.Error("score policy/order",body)}
            texts:=body["text_2"].([]any);if texts[0]!="first"||texts[1]!="second"{t.Error("candidate text changed")}
            raw:=`{"model":"owned-model","data":[{"index":1,"score":-3.5},{"index":0,"score":7.2}],"usage":{"prompt_tokens":16,"total_tokens":16}}`
            switch mode{
            case "missing":raw=strings.Replace(raw,`{"index":1,"score":-3.5},`,"",1)
            case "duplicate-index":raw=strings.Replace(raw,`"index":1`,`"index":0`,1)
            case "null-score":raw=strings.Replace(raw,`-3.5`,`null`,1)
            case "array-score":raw=strings.Replace(raw,`-3.5`,`[1,2]`,1)
            case "duplicate-field":raw=strings.Replace(raw,`"score":7.2`,`"score":7.2,"score":0`,1)
            case "wrong-model":raw=strings.Replace(raw,"owned-model","wrong",1)
            case "wrong-usage":raw=strings.Replace(raw,`"prompt_tokens":16`,`"prompt_tokens":15`,1)
            case "oversize":raw=strings.Repeat(" ",2<<20)+raw
            case "http-failure":w.WriteHeader(503)
            }
            fmt.Fprint(w,raw)
        }));defer server.Close()
        input:=proto.Clone(r).(*rerankingv1.RerankRequest)
        if mode=="wrong-space"{input.ExpectedRerankerProfileId="rp_"+strings.Repeat("0",64)}
        ctx,cancel:=context.WithCancel(context.Background());defer cancel();if mode=="cancelled"{cancel()}
        out,e:=Infer(ctx,server.Client(),server.URL,p,input)
        if mode=="ok"{if e!=nil || out.Data[0].Id!="a" || out.Data[0].Score!=7.2 || out.Data[1].Score!=-3.5 || out.Data[0].InputTokens!=8{t.Fatal(out,e)}}else if e==nil{t.Fatal("bad response accepted",mode)}
        if mode=="wrong-space" && (calls!=0 || status.Code(e)!=codes.FailedPrecondition){t.Fatal("mismatch inferred")}
        if strings.HasPrefix(mode,"long-") && (scoring!=0 || !strings.HasPrefix(status.Convert(e).Message(),"RERANK_INPUT_TOO_LONG:")){t.Fatal("long input scored",e)}
        if strings.HasPrefix(mode,"bad-token") || mode=="null-token"{if scoring!=0{t.Fatal("invalid tokenizer reached scoring")}}
    })}
}
