// Package embedding implements the Model-owned text embedding wire contract.
package embedding

import (
    "bytes"
    "context"
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "io"
    "math"
    "net"
    "net/http"
    "regexp"
    "strings"
    "time"
    "unicode/utf8"

    embeddingv1 "model-platform/gen/embedding/v1"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

const ProfileID = "snowflake-ko-v1"
const Repository = "dragonkue/snowflake-arctic-embed-l-v2.0-ko"
const MaxBatch = 16
const MaxInputBytes = 128 << 10

func failure(code codes.Code, reason, message string) error {
    return status.Error(code, reason+": "+message)
}

// No deployment/container identity or mutable display name belongs to the space.
func SpaceID(p *embeddingv1.EmbeddingProfile) string {
    raw, _ := json.Marshal([]any{"embedding-space-v1", p.ProfileId, p.SourceManifestSha256,
        p.TokenizerSha256, p.RuntimeImageId, p.QueryPrefix, p.DocumentPrefix, p.Pooling,
        p.Normalization, p.Dimension, p.MaxInputTokens, p.Truncation, "raw-utf8+special-tokens+token-id-input-v1", "dtype-auto"})
    digest := sha256.Sum256(raw)
    return "es_"+hex.EncodeToString(digest[:])
}

func ValidateRequest(req *embeddingv1.EmbedRequest) error {
    if req.GetRole() != "query" && req.GetRole() != "document" { return failure(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST","role must be query or document") }
    if !regexp.MustCompile(`^es_[a-f0-9]{64}$`).MatchString(req.GetExpectedEmbeddingSpaceId()) { return failure(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST","expected_embedding_space_id is required") }
    if len(req.GetInputs()) < 1 || len(req.GetInputs()) > MaxBatch { return failure(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST","batch must contain 1..16 inputs") }
    seen, size := map[string]bool{}, 0
    identifier := regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$`)
    for _, input := range req.GetInputs() {
        if !identifier.MatchString(input.GetId()) || seen[input.GetId()] || !utf8.ValidString(input.GetText()) || strings.TrimSpace(input.GetText()) == "" { return failure(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST","inputs require unique IDs and nonempty UTF-8 text") }
        seen[input.GetId()] = true
        size += len(input.GetText())
        if size > MaxInputBytes { return failure(codes.InvalidArgument,"EMBEDDING_INPUT_TOO_LARGE","input text exceeds 128 KiB") }
    }
    return nil
}

func NewHTTPClient() *http.Client {
    return &http.Client{Timeout:110*time.Second,
        Transport:&http.Transport{Proxy:nil, DialContext:(&net.Dialer{Timeout:5*time.Second,KeepAlive:30*time.Second}).DialContext,
            MaxIdleConns:16, IdleConnTimeout:90*time.Second, ResponseHeaderTimeout:105*time.Second},
        CheckRedirect:func(*http.Request,[]*http.Request)error{return http.ErrUseLastResponse}}
}

func post(ctx context.Context, client *http.Client, endpoint string, body, result any, tokenize bool) error {
    raw, err := json.Marshal(body)
    if err != nil { return failure(codes.Internal,"EMBEDDING_INTERNAL","request encoding failed") }
    request, err := http.NewRequestWithContext(ctx,http.MethodPost,endpoint,bytes.NewReader(raw))
    if err != nil { return failure(codes.Internal,"EMBEDDING_INTERNAL","invalid owned runtime endpoint") }
    request.Header.Set("Content-Type","application/json")
    response, err := client.Do(request)
    if err != nil {
        if ctx.Err() != nil { return status.FromContextError(ctx.Err()).Err() }
        if e,ok:=err.(net.Error); ok && e.Timeout() { return failure(codes.DeadlineExceeded,"EMBEDDING_TIMEOUT","runtime timed out") }
        return failure(codes.Unavailable,"EMBEDDING_UNAVAILABLE","runtime request failed")
    }
    defer response.Body.Close()
    if response.StatusCode != http.StatusOK {
        if response.StatusCode==400 || response.StatusCode==413 || response.StatusCode==422 { return failure(codes.InvalidArgument,"EMBEDDING_INPUT_REJECTED","runtime rejected input; no result was accepted") }
        if tokenize && response.StatusCode==404 { return failure(codes.FailedPrecondition,"EMBEDDING_TOKENIZER_UNAVAILABLE","runtime must expose /tokenize") }
        return failure(codes.Unavailable,"EMBEDDING_UNAVAILABLE","runtime returned an unsuccessful response")
    }
    data,err:=io.ReadAll(io.LimitReader(response.Body,(2<<20)+1))
    if err!=nil || len(data)>2<<20 || json.Unmarshal(data,result)!=nil { return failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","malformed or oversized runtime response") }
    return nil
}

// Infer never discovers a URL: the Deployment service supplies its owned origin.
// All inputs are tokenized/checked before any embedding batch is submitted.
func Infer(ctx context.Context, client *http.Client, origin string, profile *embeddingv1.EmbeddingProfile, req *embeddingv1.EmbedRequest) (*embeddingv1.EmbedResponse,error) {
    if err:=ValidateRequest(req);err!=nil{return nil,err}
    if req.ExpectedEmbeddingSpaceId != profile.EmbeddingSpaceId { return nil,failure(codes.FailedPrecondition,"EMBEDDING_SPACE_MISMATCH","profile changed; resolve the intended space before retrying") }
    tokenInputs:=make([][]int,len(req.Inputs))
    for i,input:=range req.Inputs {
        prefix:=profile.DocumentPrefix
        if req.Role=="query"{prefix=profile.QueryPrefix}
        var result struct{Count int `json:"count"`;MaxModelLen int32 `json:"max_model_len"`;Tokens []*int `json:"tokens"`}
        body:=map[string]any{"model":profile.ServedModelName,"prompt":prefix+input.Text,"add_special_tokens":true}
        if err:=post(ctx,client,origin+"/tokenize",body,&result,true);err!=nil{return nil,err}
        if result.Count<=0 || result.Count!=len(result.Tokens) || result.MaxModelLen!=profile.MaxInputTokens { return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","tokenizer count or runtime limit mismatch") }
        if result.Count>int(profile.MaxInputTokens){return nil,failure(codes.InvalidArgument,"EMBEDDING_INPUT_TOO_LONG","input "+input.Id+" exceeds max_input_tokens; truncation is disabled")}
        tokenInputs[i]=make([]int,len(result.Tokens))
        for j,token:=range result.Tokens{
            if token==nil || *token<0 || *token>=250002{return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","invalid tokenizer token ID")}
            tokenInputs[i][j]=*token
        }
    }
    var result struct{
        Model string `json:"model"`
        Data []struct{Index *int `json:"index"`;Vector []*float64 `json:"embedding"`} `json:"data"`
    }
    body:=map[string]any{"model":profile.ServedModelName,"input":tokenInputs,"encoding_format":"float"}
    if err:=post(ctx,client,origin+"/v1/embeddings",body,&result,false);err!=nil{return nil,err}
    if result.Model!=profile.ServedModelName || len(result.Data)!=len(req.Inputs){return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","model or output count mismatch")}
    outputs:=make([]*embeddingv1.EmbeddingOutput,len(req.Inputs))
    for _,row:=range result.Data {
        if row.Index==nil || *row.Index<0 || *row.Index>=len(outputs) || outputs[*row.Index]!=nil || len(row.Vector)!=int(profile.Dimension){return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","output index or dimension mismatch")}
        vector:=make([]float64,len(row.Vector));norm:=0.0
        for i,value:=range row.Vector {
            if value==nil || math.IsNaN(*value) || math.IsInf(*value,0){return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","nonfinite/null vector")}
            vector[i]=*value;norm+=(*value)*(*value)
        }
        if math.IsNaN(norm) || math.IsInf(norm,0) || math.Abs(math.Sqrt(norm)-1)>0.001{return nil,failure(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE","vector is not L2 normalized")}
        i:=*row.Index
        outputs[i]=&embeddingv1.EmbeddingOutput{Id:req.Inputs[i].Id,Index:int32(i),Embedding:vector,InputTokens:int32(len(tokenInputs[i]))}
    }
    return &embeddingv1.EmbedResponse{Profile:profile,Role:req.Role,Data:outputs},nil
}
