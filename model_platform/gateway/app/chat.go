package app

import (
    "bytes"
    "context"
    "encoding/json"
    "fmt"
    "io"
    "net"
    "net/http"
    "strings"
    "time"
    "unicode/utf8"

    deploymentv1 "model-platform/gen/deployment/v1"
    managementv1 "model-platform/gen/management/v1"
    "model-platform/internal/embedding"
    "model-platform/internal/reranking"
    "github.com/gofiber/fiber/v3"
    "github.com/google/uuid"
)

type chatMessage struct {
    Role string `json:"role"`
    Content string `json:"content"`
}
type chatBody struct {
    Messages []chatMessage `json:"messages"`
    MaxTokens int `json:"max_tokens"`
    Temperature float64 `json:"temperature"`
}

func newChatHTTPClient() *http.Client {
    return &http.Client{
        Timeout: 120*time.Second,
        Transport: &http.Transport{Proxy:nil, DialContext:(&net.Dialer{Timeout:5*time.Second,KeepAlive:30*time.Second}).DialContext,
            MaxIdleConns:16, IdleConnTimeout:90*time.Second, ResponseHeaderTimeout:110*time.Second},
        CheckRedirect:func(*http.Request,[]*http.Request)error{return http.ErrUseLastResponse},
    }
}

func parseChat(raw []byte)(chatBody,error){
    body:=chatBody{MaxTokens:256,Temperature:0.7}
    if len(raw)>65536 || !utf8.Valid(raw){return body,fmt.Errorf("chat body must be UTF-8 and at most 64 KiB")}
    object,err:=uniqueEvaluationObject(raw);if err!=nil{return body,err}
    for key,value:=range object{
        if key!="messages"&&key!="max_tokens"&&key!="temperature"{return body,fmt.Errorf("unknown chat field: %s",key)}
        if bytes.Equal(bytes.TrimSpace(value),[]byte("null")){return body,fmt.Errorf("chat fields cannot be null")}
    }
    if err=json.Unmarshal(raw,&body);err!=nil{return body,fmt.Errorf("invalid chat field type")}
    if len(body.Messages)<1 || len(body.Messages)>33 || body.MaxTokens<1 || body.MaxTokens>512 || body.Temperature<0 || body.Temperature>1.5{return body,fmt.Errorf("chat request exceeds supported limits")}
    var rawMessages []json.RawMessage
    if err=json.Unmarshal(object["messages"],&rawMessages);err!=nil{return body,fmt.Errorf("messages are required")}
    next:="user";size:=0
    for i,m:=range body.Messages{
        fields,e:=uniqueEvaluationObject(rawMessages[i]);if e!=nil||len(fields)!=2||fields["role"]==nil||fields["content"]==nil{return body,fmt.Errorf("messages accept exactly role and content")}
        if strings.TrimSpace(m.Content)==""{return body,fmt.Errorf("empty message")}
        size+=utf8.RuneCountInString(m.Content)
        if i==0&&m.Role=="system"{continue}
        if m.Role!=next{return body,fmt.Errorf("messages must alternate user and assistant")}
        if next=="user"{next="assistant"}else{next="user"}
    }
    if body.Messages[len(body.Messages)-1].Role!="user"||size>16000{return body,fmt.Errorf("last message must be user; total text limit is 16000 characters")}
    return body,nil
}

func chatError(c fiber.Ctx,httpStatus int,code,message string)error{
    return c.Status(httpStatus).JSON(fiber.Map{"code":code,"error":message})
}

// This is an inference request, not an Operation or a generic HTTP proxy.
// Destination and model alias come exclusively from the persisted deployment.
func(a *API)chatDeployment(c fiber.Ctx)error{
    id,err:=uuid.Parse(c.Params("id"))
    if err!=nil||id==uuid.Nil||len(c.Request().URI().QueryString())!=0{return chatError(c,400,"INVALID_CHAT","invalid deployment ID or query")}
    body,err:=parseChat(c.Body());if err!=nil{return chatError(c,400,"INVALID_CHAT",err.Error())}
    ctx,cancel:=context.WithTimeout(c.Context(),125*time.Second);defer cancel()
    lookup,lookupCancel:=context.WithTimeout(ctx,15*time.Second)
    dep,err:=a.clients.Deployment.GetDeployment(lookup,&deploymentv1.GetDeploymentRequest{DeploymentId:id.String()});lookupCancel()
    if err!=nil{return grpcError(c,err)}
    if dep.GetDeploymentId()!=id.String()||dep.GetStatus()!="ready"||dep.GetRuntimeState()!="ready"||dep.GetDesiredState()!="running"||dep.GetServing().GetEngine()!="vllm"||dep.GetServing().GetProfile()!=""{
        return chatError(c,409,"DEPLOYMENT_NOT_READY","deployment is not ready for chat")
    }
    modelContext, modelCancel := context.WithTimeout(ctx, 10*time.Second)
    defer modelCancel()
    version, err := a.clients.Management.GetModelVersion(modelContext, &managementv1.GetModelVersionRequest{ModelVersionId:dep.GetModelVersionId()})
    if err != nil { return grpcError(c,err) }
    if version.GetModelVersionId()!=dep.GetModelVersionId() || version.GetModelId()=="" || (version.GetRepoId()==embedding.Repository || version.GetRepoId()==reranking.Repository) {
        return chatError(c,409,"CHAT_CAPABILITY_REQUIRED","selected model is not a chat deployment")
    }
    model, err := a.clients.Management.GetModel(modelContext,&managementv1.GetModelRequest{ModelId:version.GetModelId()})
    if err != nil { return grpcError(c,err) }
    if model.GetModelId()!=version.GetModelId() || model.GetModelType()!="llm" {
        return chatError(c,409,"CHAT_CAPABILITY_REQUIRED","registered model is not a chat LLM")
    }
    alias:="deployment-"+id.String()
    endpoint:="http://model-serving-"+id.String()+":8000/v1"
    if dep.GetInternalEndpoint()!=endpoint||dep.GetServedModelName()!=alias{
        return chatError(c,502,"INVALID_RUNTIME_IDENTITY","unexpected local serving endpoint or model")
    }
    payload,_:=json.Marshal(struct{
        chatBody
        Model string `json:"model"`
        Stream bool `json:"stream"`
    }{body,alias,false})
    request,err:=http.NewRequestWithContext(ctx,http.MethodPost,endpoint+"/chat/completions",bytes.NewReader(payload))
    if err!=nil{return chatError(c,502,"INVALID_RUNTIME_IDENTITY","invalid runtime request")}
    request.Header.Set("Content-Type","application/json")
    response,err:=a.chatHTTP.Do(request)
    if err!=nil{
        if e,ok:=err.(net.Error);ok&&e.Timeout(){return chatError(c,504,"INFERENCE_TIMEOUT","inference timed out")}
        return chatError(c,503,"INFERENCE_UNAVAILABLE","inference runtime is unavailable")
    }
    defer response.Body.Close()
    if response.StatusCode==400||response.StatusCode==413||response.StatusCode==422{
        return chatError(c,400,"CHAT_INPUT_REJECTED","runtime rejected the conversation; shorten the input or start a new conversation")
    }
    if response.StatusCode!=200{return chatError(c,502,"INFERENCE_UNAVAILABLE","runtime returned an unsuccessful response")}
    raw,err:=io.ReadAll(io.LimitReader(response.Body,(1<<20)+1))
    if err!=nil||len(raw)>1<<20{return chatError(c,502,"INVALID_INFERENCE_RESPONSE","invalid runtime response")}
    var answer struct{
        Model string `json:"model"`
        Choices []struct{Message chatMessage `json:"message"`;FinishReason string `json:"finish_reason"`} `json:"choices"`
        Usage struct{PromptTokens int64 `json:"prompt_tokens"`;CompletionTokens int64 `json:"completion_tokens"`;TotalTokens int64 `json:"total_tokens"`} `json:"usage"`
    }
    if json.Unmarshal(raw,&answer)!=nil||answer.Model!=alias||len(answer.Choices)!=1||answer.Choices[0].Message.Role!="assistant"||strings.TrimSpace(answer.Choices[0].Message.Content)==""{
        return chatError(c,502,"INVALID_INFERENCE_RESPONSE","runtime did not answer as the requested model")
    }
    if answer.Choices[0].FinishReason!="stop"&&answer.Choices[0].FinishReason!="length"{return chatError(c,502,"INVALID_INFERENCE_RESPONSE","unsupported completion result")}
    return c.JSON(fiber.Map{"deployment_id":id.String(),"model":answer.Model,"content":answer.Choices[0].Message.Content,"finish_reason":answer.Choices[0].FinishReason,"usage":answer.Usage})
}
