package app

import (
    "context"
    "encoding/json"
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"

    "model-platform/gateway/grpcclient"
    deploymentv1 "model-platform/gen/deployment/v1"
    managementv1 "model-platform/gen/management/v1"
    "model-platform/internal/embedding"
    "model-platform/internal/reranking"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

const chatTestID="d8be0fc3-6231-4a89-9360-78fc7404deaa"
type chatDeploymentClient struct{
    deploymentv1.DeploymentServiceClient
    deployment *deploymentv1.Deployment
    err error
    calls int
}
func(c *chatDeploymentClient)GetDeployment(_ context.Context,r *deploymentv1.GetDeploymentRequest,_ ...grpc.CallOption)(*deploymentv1.Deployment,error){
    c.calls++;if r.GetDeploymentId()!=chatTestID{panic("wrong lookup ID")};return c.deployment,c.err
}
type chatRoundTrip func(*http.Request)(*http.Response,error)
func(f chatRoundTrip)RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
func readyChatDeployment()*deploymentv1.Deployment{return &deploymentv1.Deployment{
    DeploymentId:chatTestID,ModelVersionId:chatTestID,Status:"ready",RuntimeState:"ready",DesiredState:"running",
    Serving:&deploymentv1.ServingSpec{Engine:"vllm"},
    InternalEndpoint:"http://model-serving-"+chatTestID+":8000/v1",ServedModelName:"deployment-"+chatTestID,
}}
func chatResponse(code int,body string)*http.Response{return &http.Response{StatusCode:code,Body:io.NopCloser(strings.NewReader(body)),Header:make(http.Header)}}
func chatApp(client *chatDeploymentClient,transport chatRoundTrip)*fiber.App{
    app:=fiber.New();api:=New(&grpcclient.Clients{Deployment:client,Management:&chatManagementClient{}});api.chatHTTP=&http.Client{Transport:transport};api.Register(app);return app
}
func callChat(t *testing.T,app *fiber.App,path,body string)(int,map[string]any){
    t.Helper();request:=httptest.NewRequest("POST",path,strings.NewReader(body));request.Header.Set("Content-Type","application/json")
    response,err:=app.Test(request);if err!=nil{t.Fatal(err)};defer response.Body.Close()
    out:=map[string]any{};if err=json.NewDecoder(response.Body).Decode(&out);err!=nil{t.Fatal(err)};return response.StatusCode,out
}

func TestChatUsesPersistedRuntimeAndAdapterAlias(t *testing.T){
    client:=&chatDeploymentClient{deployment:readyChatDeployment()};calls:=0
    app:=chatApp(client,func(r *http.Request)(*http.Response,error){
        calls++;if r.URL.String()!="http://model-serving-"+chatTestID+":8000/v1/chat/completions"||r.Method!="POST"{t.Fatal("wrong destination",r.URL)}
        var payload map[string]any;if err:=json.NewDecoder(r.Body).Decode(&payload);err!=nil{t.Fatal(err)}
        if payload["model"]!="deployment-"+chatTestID||payload["stream"]!=false||payload["max_tokens"]!=float64(256)||payload["temperature"]!=0.7{t.Fatal(payload)}
        messages:=payload["messages"].([]any);if messages[0].(map[string]any)["content"]!="안녕하세요"{t.Fatal(payload)}
        return chatResponse(200,`{"model":"deployment-`+chatTestID+`","choices":[{"message":{"role":"assistant","content":"Hello!"},"finish_reason":"stop"}],"usage":{"prompt_tokens":15,"completion_tokens":3,"total_tokens":18}}`),nil
    })
    code,out:=callChat(t,app,"/api/deployments/"+chatTestID+"/chat",`{"messages":[{"role":"user","content":"안녕하세요"}]}`)
    if code!=200||out["content"]!="Hello!"||out["deployment_id"]!=chatTestID||calls!=1||client.calls!=1{t.Fatal(code,out,calls)}
}

func TestChatRejectsMalformedInputBeforeLookup(t *testing.T){
    client:=&chatDeploymentClient{deployment:readyChatDeployment()}
    app:=chatApp(client,func(*http.Request)(*http.Response,error){t.Fatal("unexpected inference");return nil,nil})
    for _,body:=range []string{
        `{}`,`{"messages":null}`,`{"messages":[]}`,`{"messages":[],"messages":[]}`,
        `{"messages":[{"role":"user","content":"Q"}],"model":"base"}`,
        `{"messages":[{"role":"user","content":"Q"}],"stream":true}`,
        `{"messages":[{"Role":"user","content":"Q"}]}`,
        `{"messages":[{"role":"user","content":"Q","content":"X"}]}`,
        `{"messages":[{"role":"user","content":"Q"}],"max_tokens":null}`,
        `{"messages":[{"role":"user","content":"Q"}],"max_tokens":513}`,
        `{"messages":[{"role":"user","content":"Q"}],"temperature":-1}`,
        `{"messages":[{"role":"assistant","content":"Q"}]}`,
        `{"messages":[{"role":"user","content":"Q"},{"role":"user","content":"Q2"}]}`,
        `{"messages":[{"role":"user","content":"Q"},{"role":"assistant","content":"A"}]}`,
        `{"messages":[{"role":"user","content":"`+strings.Repeat("x",16001)+`"}]}`,
        strings.Repeat("x",65537),
    }{code,_:=callChat(t,app,"/api/deployments/"+chatTestID+"/chat",body);if code!=400{t.Fatalf("got %d for %.120s",code,body)}}
    if client.calls!=0{t.Fatal("invalid request reached deployment service")}
}

func TestChatRequiresReadyLocalIdentity(t *testing.T){
    for _,name:=range []string{"queued","stopped","desired-stop","wrong-alias","external-url","wrong-id","non-vllm","lookup-unavailable"}{
        t.Run(name,func(t *testing.T){
            dep:=readyChatDeployment();client:=&chatDeploymentClient{deployment:dep};expected:=409
            switch name{
            case "queued":dep.RuntimeState="queued"
            case "stopped":dep.Status="stopped"
            case "desired-stop":dep.DesiredState="stopped"
            case "wrong-alias":dep.ServedModelName+="-base";expected=502
            case "external-url":dep.InternalEndpoint="http://127.0.0.1:5000/v1";expected=502
            case "wrong-id":dep.DeploymentId="other"
            case "non-vllm":dep.Serving.Engine="demo"
            case "lookup-unavailable":client.err=status.Error(codes.Unavailable,"offline");expected=503
            }
            app:=chatApp(client,func(*http.Request)(*http.Response,error){t.Fatal("unsafe inference call");return nil,nil})
            code,_:=callChat(t,app,"/api/deployments/"+chatTestID+"/chat",`{"messages":[{"role":"user","content":"Q"}]}`)
            if code!=expected{t.Fatal(code,expected)}
        })
    }
}

func TestChatRejectsBaseResponseAndUpstreamFailures(t *testing.T){
    for _,test:=range []struct{name string;upstream int;raw string;expected int}{
        {"base",200,`{"model":"deployment-`+chatTestID+`-base","choices":[{"message":{"role":"assistant","content":"Hello"},"finish_reason":"stop"}]}`,502},
        {"empty",200,`{}`,502},{"html",200,"<html>bad</html>",502},
        {"oversize",200,strings.Repeat("x",(1<<20)+1),502},
        {"context",400,"private runtime details",400},{"redirect",302,"",502},{"unavailable",503,"private path",502},
    }{t.Run(test.name,func(t *testing.T){
        app:=chatApp(&chatDeploymentClient{deployment:readyChatDeployment()},func(*http.Request)(*http.Response,error){return chatResponse(test.upstream,test.raw),nil})
        code,out:=callChat(t,app,"/api/deployments/"+chatTestID+"/chat",`{"messages":[{"role":"user","content":"Q"}]}`)
        if code!=test.expected||strings.Contains(out["error"].(string),"private"){t.Fatal(code,out)}
    })}
}

func TestChatTransportDoesNotFollowRedirectsOrEnvironmentProxy(t *testing.T){
    client:=newChatHTTPClient()
    if client.CheckRedirect(nil,nil)!=http.ErrUseLastResponse||client.Timeout==0||client.Transport.(*http.Transport).Proxy!=nil{t.Fatal("unsafe chat transport")}
}

type chatManagementClient struct{managementv1.ManagementServiceClient; repo,kind string}
func(c *chatManagementClient)GetModelVersion(_ context.Context,r *managementv1.GetModelVersionRequest,_ ...grpc.CallOption)(*managementv1.ModelVersion,error){return &managementv1.ModelVersion{ModelVersionId:r.ModelVersionId,ModelId:chatTestID,RepoId:c.repo},nil}
func(c *chatManagementClient)GetModel(_ context.Context,r *managementv1.GetModelRequest,_ ...grpc.CallOption)(*managementv1.Model,error){kind:=c.kind;if kind==""{kind="llm"};return &managementv1.Model{ModelId:r.ModelId,ModelType:kind},nil}

func TestChatRejectsLegacyEmbeddingBeforeInference(t *testing.T){
    for _,metadata:=range []*chatManagementClient{{repo:embedding.Repository},{repo:reranking.Repository},{kind:"embedding"},{kind:"reranker"}}{
        app:=fiber.New();api:=New(&grpcclient.Clients{Deployment:&chatDeploymentClient{deployment:readyChatDeployment()},Management:metadata})
        api.chatHTTP=&http.Client{Transport:chatRoundTrip(func(*http.Request)(*http.Response,error){t.Fatal("embedding routed to chat");return nil,nil})};api.Register(app)
        code,reply:=callChat(t,app,"/api/deployments/"+chatTestID+"/chat",`{"messages":[{"role":"user","content":"Q"}]}`)
        if code!=409 || reply["code"]!="CHAT_CAPABILITY_REQUIRED"{t.Fatal(code,reply)}
    }
}

