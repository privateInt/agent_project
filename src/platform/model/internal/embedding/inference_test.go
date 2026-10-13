package embedding

import (
    "context"
    "encoding/json"
    "io"
    "net/http"
    "strings"
    "testing"

    embeddingv1 "model-platform/gen/embedding/v1"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/proto"
)

type roundTrip func(*http.Request)(*http.Response,error)
func(f roundTrip)RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
func reply(code int,body any)*http.Response{raw,_:=json.Marshal(body);return &http.Response{StatusCode:code,Body:io.NopCloser(strings.NewReader(string(raw))),Header:make(http.Header)}}
func fixture()(*embeddingv1.EmbeddingProfile,*embeddingv1.EmbedRequest){
    p:=&embeddingv1.EmbeddingProfile{DeploymentId:"deployment-one",ProfileId:"snowflake-ko-v1",SourceManifestSha256:strings.Repeat("a",64),TokenizerSha256:strings.Repeat("b",64),RuntimeImageId:"sha256:"+strings.Repeat("c",64),QueryPrefix:"query: ",Pooling:"cls",Normalization:"l2",Dimension:1024,MaxInputTokens:2048,Truncation:"reject",ServedModelName:"model-one"}
    p.EmbeddingSpaceId=SpaceID(p)
    return p,&embeddingv1.EmbedRequest{DeploymentId:p.DeploymentId,ExpectedEmbeddingSpaceId:p.EmbeddingSpaceId,Role:"query",Inputs:[]*embeddingv1.EmbeddingInput{{Id:"q1",Text:"서울?"},{Id:"q2",Text:"  공백 유지  "}}}
}
func unitVector()[]float64{v:=make([]float64,1024);v[0]=1;return v}

func TestSpaceIdentityExcludesDeploymentAndIncludesSemanticContract(t *testing.T){
    p,_:=fixture();original:=SpaceID(p)
    copy:=proto.Clone(p).(*embeddingv1.EmbeddingProfile)
    copy.DeploymentId="other";copy.RuntimeContainerId="other";copy.ModelVersionId="other";copy.ServingProfile="new-deployment-profile"
    if SpaceID(copy)!=original{t.Fatal("redeployment changed space")}
    for _,change:=range []func(*embeddingv1.EmbeddingProfile){func(p *embeddingv1.EmbeddingProfile){p.QueryPrefix="other"},func(p *embeddingv1.EmbeddingProfile){p.TokenizerSha256="other"},func(p *embeddingv1.EmbeddingProfile){p.SourceManifestSha256="other"},func(p *embeddingv1.EmbeddingProfile){p.RuntimeImageId="other"},func(p *embeddingv1.EmbeddingProfile){p.Dimension=768},func(p *embeddingv1.EmbeddingProfile){p.MaxInputTokens=4096}}{
        copy=proto.Clone(p).(*embeddingv1.EmbeddingProfile);change(copy);if SpaceID(copy)==original{t.Fatal("changed contract reused space")}
    }
}

func TestRolesTokensAndReorderedOutput(t *testing.T){
    for _,role:=range []string{"query","document"}{t.Run(role,func(t *testing.T){
        p,r:=fixture();r.Role=role;calls:=0
        client:=&http.Client{Transport:roundTrip(func(req *http.Request)(*http.Response,error){
            var body map[string]json.RawMessage
            if json.NewDecoder(req.Body).Decode(&body)!=nil{t.Fatal("bad payload")}
            calls++
            if req.URL.Path=="/tokenize"{
                var prompt string;json.Unmarshal(body["prompt"],&prompt)
                expected:=r.Inputs[calls-1].Text;if role=="query"{expected="query: "+expected}
                if prompt!=expected || string(body["add_special_tokens"])!="true"{t.Fatal("incorrect preprocessing",prompt)}
                return reply(200,map[string]any{"count":3,"max_model_len":2048,"tokens":[]int{0,10+calls,2}}),nil
            }
            var tokens [][]int
            if req.URL.Path!="/v1/embeddings" || json.Unmarshal(body["input"],&tokens)!=nil || len(tokens)!=2 || tokens[0][1]!=11 || tokens[1][1]!=12 || len(body)!=3{t.Fatal("did not reuse exact token IDs")}
            return reply(200,map[string]any{"model":p.ServedModelName,"data":[]any{map[string]any{"index":1,"embedding":unitVector()},map[string]any{"index":0,"embedding":unitVector()}}}),nil
        })}
        out,err:=Infer(context.Background(),client,"http://owned",p,r)
        if err!=nil || calls!=3 || out.Data[0].Id!="q1" || out.Data[1].Id!="q2" || out.Data[0].InputTokens!=3{t.Fatal(out,err,calls)}
    })}
}

func TestInvalidRequestsAndChangedSpaceNeverCallRuntime(t *testing.T){
    p,r:=fixture()
    client:=&http.Client{Transport:roundTrip(func(*http.Request)(*http.Response,error){t.Fatal("unexpected HTTP");return nil,nil})}
    for _,change:=range []func(*embeddingv1.EmbedRequest){
        func(r *embeddingv1.EmbedRequest){r.Inputs=nil},func(r *embeddingv1.EmbedRequest){r.Inputs[1].Id="q1"},func(r *embeddingv1.EmbedRequest){r.Role="other"},func(r *embeddingv1.EmbedRequest){r.Inputs[0].Text="  "},func(r *embeddingv1.EmbedRequest){r.Inputs[0].Text=string([]byte{0xff})},func(r *embeddingv1.EmbedRequest){r.Inputs[0].Text=strings.Repeat("x",MaxInputBytes+1)},func(r *embeddingv1.EmbedRequest){r.ExpectedEmbeddingSpaceId="es_"+strings.Repeat("0",64)},
    }{
        copy:=proto.Clone(r).(*embeddingv1.EmbedRequest);change(copy)
        if _,err:=Infer(context.Background(),client,"http://owned",p,copy);err==nil{t.Fatal("invalid input accepted")}
    }
}

func TestOverlongOrBadTokenizationNeverSubmitsEmbeddings(t *testing.T){
    for _,kind:=range []string{"long","count","limit","null-token","token-range","not-supported"}{t.Run(kind,func(t *testing.T){
        p,r:=fixture();calls:=0
        client:=&http.Client{Transport:roundTrip(func(req *http.Request)(*http.Response,error){
            calls++;if req.URL.Path!="/tokenize"{t.Fatal("overlong batch was submitted")}
            body:=map[string]any{"count":3,"max_model_len":2048,"tokens":[]int{0,1,2}}
            switch kind{case "long":body["count"]=2049;body["tokens"]=make([]int,2049);case "count":body["count"]=2;case "limit":body["max_model_len"]=8192;case "null-token":body["tokens"]=[]any{0,nil,2};case "token-range":body["tokens"]=[]int{0,250002,2};case "not-supported":return reply(404,map[string]any{}),nil}
            return reply(200,body),nil
        })}
        if _,err:=Infer(context.Background(),client,"http://owned",p,r);err==nil{t.Fatal("bad tokenizer accepted")}
        if calls!=1{t.Fatal("should fail entire request immediately",calls)}
    })}
}

func TestMalformedEmbeddingResponseIsNeverPartialSuccess(t *testing.T){
    for _,kind:=range []string{"wrong-model","missing","duplicate","range","no-index","dimension","null-vector-value","norm","overflow","timeout"}{t.Run(kind,func(t *testing.T){
        p,r:=fixture()
        client:=&http.Client{Transport:roundTrip(func(req *http.Request)(*http.Response,error){
            if req.URL.Path=="/tokenize"{return reply(200,map[string]any{"count":3,"max_model_len":2048,"tokens":[]int{0,1,2}}),nil}
            a:=map[string]any{"index":0,"embedding":unitVector()};b:=map[string]any{"index":1,"embedding":unitVector()}
            body:=map[string]any{"model":p.ServedModelName,"data":[]any{a,b}}
            switch kind{
            case "wrong-model":body["model"]="base"
            case "missing":body["data"]=[]any{a}
            case "duplicate":b["index"]=0
            case "range":b["index"]=2
            case "no-index":delete(a,"index")
            case "dimension":a["embedding"]=[]float64{1}
            case "null-vector-value":v:=make([]any,1024);for i:=range v{v[i]=0.0};v[0]=1.0;v[1]=nil;a["embedding"]=v
            case "norm":a["embedding"]=make([]float64,1024)
            case "overflow":v:=unitVector();v[0]=1e300;a["embedding"]=v
            case "timeout":return nil,context.DeadlineExceeded
            }
            return reply(200,body),nil
        })}
        out,err:=Infer(context.Background(),client,"http://owned",p,r)
        if err==nil || out!=nil{t.Fatal("accepted corrupt/partial result")}
        if kind!="timeout" && status.Code(err)!=codes.DataLoss{t.Fatal("wrong failure classification",err)}
    })}
}
