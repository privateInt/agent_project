package reranking
import("bytes";"context";"encoding/json";"io";"net";"net/http";"time";"google.golang.org/grpc/codes";"google.golang.org/grpc/status")
func NewHTTPClient() *http.Client {
    return &http.Client{Timeout:110*time.Second,
        Transport:&http.Transport{Proxy:nil, DialContext:(&net.Dialer{Timeout:5*time.Second,KeepAlive:30*time.Second}).DialContext,
            MaxIdleConns:16, IdleConnTimeout:90*time.Second, ResponseHeaderTimeout:105*time.Second},
        CheckRedirect:func(*http.Request,[]*http.Request)error{return http.ErrUseLastResponse}}
}

func post(ctx context.Context, client *http.Client, endpoint string, body, result any, tokenize bool) error {
    raw, err := json.Marshal(body)
    if err != nil { return failure(codes.Internal,"RERANK_INTERNAL","request encoding failed") }
    request, err := http.NewRequestWithContext(ctx,http.MethodPost,endpoint,bytes.NewReader(raw))
    if err != nil { return failure(codes.Internal,"RERANK_INTERNAL","invalid owned runtime endpoint") }
    request.Header.Set("Content-Type","application/json")
    response, err := client.Do(request)
    if err != nil {
        if ctx.Err() != nil { return status.FromContextError(ctx.Err()).Err() }
        if e,ok:=err.(net.Error); ok && e.Timeout() { return failure(codes.DeadlineExceeded,"RERANK_TIMEOUT","runtime timed out") }
        return failure(codes.Unavailable,"RERANK_UNAVAILABLE","runtime request failed")
    }
    defer response.Body.Close()
    if response.StatusCode != http.StatusOK {
        if response.StatusCode==400 || response.StatusCode==413 || response.StatusCode==422 { return failure(codes.InvalidArgument,"RERANK_INPUT_REJECTED","runtime rejected input; no result was accepted") }
        if tokenize && response.StatusCode==404 { return failure(codes.FailedPrecondition,"RERANK_TOKENIZER_UNAVAILABLE","runtime must expose /tokenize") }
        return failure(codes.Unavailable,"RERANK_UNAVAILABLE","runtime returned an unsuccessful response")
    }
    data,err:=io.ReadAll(io.LimitReader(response.Body,(2<<20)+1))
    if err!=nil || len(data)>2<<20 || strictJSON(data,result)!=nil { return failure(codes.DataLoss,"RERANK_INVALID_RESPONSE","malformed or oversized runtime response") }
    return nil
}

