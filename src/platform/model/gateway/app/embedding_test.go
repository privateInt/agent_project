package app

import (
    "context"
    "encoding/json"
    "net/http/httptest"
    "strings"
    "testing"

    "model-platform/gateway/grpcclient"
    embeddingv1 "model-platform/gen/embedding/v1"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

type embeddingClient struct{
    embeddingv1.EmbeddingServiceClient
    request *embeddingv1.EmbedRequest
    err error
}
func(c *embeddingClient)GetProfile(_ context.Context,r *embeddingv1.GetProfileRequest,_ ...grpc.CallOption)(*embeddingv1.EmbeddingProfile,error){return &embeddingv1.EmbeddingProfile{DeploymentId:r.DeploymentId,Capability:"embedding",Dimension:1024},c.err}
func(c *embeddingClient)Embed(_ context.Context,r *embeddingv1.EmbedRequest,_ ...grpc.CallOption)(*embeddingv1.EmbedResponse,error){c.request=r;return &embeddingv1.EmbedResponse{Role:r.Role},c.err}

func TestEmbeddingParserRejectsURLsDuplicatesAndUnknownFields(t *testing.T){
    good:=`{"expected_embedding_space_id":"es_`+strings.Repeat("a",64)+`","role":"query","inputs":[{"id":"q1","text":"서울?"}]}`
    if r,e:=parseEmbedding([]byte(good));e!=nil || r.Inputs[0].Text!="서울?"{t.Fatal(r,e)}
    for _,bad:=range []string{
        strings.Replace(good,`"role":"query"`,`"role":"query","role":"document"`,1),
        strings.Replace(good,`"role":"query"`,`"role":"query","endpoint":"http://external"`,1),
        strings.Replace(good,`"text":"서울?"`,`"text":"서울?","model":"override"`,1),
        strings.Replace(good,`"id":"q1"`,`"id":"q1","id":"q2"`,1),
        strings.Replace(good,`"text":"서울?"`,`"text":null`,1),good+` {}`,strings.Repeat("x",(1<<20)+1),
    }{if _,e:=parseEmbedding([]byte(bad));status.Code(e)!=codes.InvalidArgument{t.Fatal("accepted invalid JSON/override",e)}}
}

func TestEmbeddingGatewayUsesIDRPCAndStableErrors(t *testing.T){
    id:="44247297-5184-4a36-9225-7791c80d3add"
    client:=&embeddingClient{};app:=fiber.New();New(&grpcclient.Clients{Embedding:client}).Register(app)
    response,err:=app.Test(httptest.NewRequest("GET","/api/deployments/"+id+"/embedding-profile",nil))
    if err!=nil{t.Fatal(err)}
    var profile map[string]any;json.NewDecoder(response.Body).Decode(&profile);response.Body.Close()
    if response.StatusCode!=200 || profile["deployment_id"]!=id || profile["dimension"]!=float64(1024){t.Fatal(response.StatusCode,profile)}
    body:=`{"expected_embedding_space_id":"es_`+strings.Repeat("a",64)+`","role":"document","inputs":[{"id":"chunk1","text":"text"}]}`
    for _,test:=range []struct{err error;code int;reason string}{
        {nil,200,""},{status.Error(codes.FailedPrecondition,"EMBEDDING_SPACE_MISMATCH: changed"),409,"EMBEDDING_SPACE_MISMATCH"},
        {status.Error(codes.DataLoss,"EMBEDDING_INVALID_RESPONSE: count"),502,"EMBEDDING_INVALID_RESPONSE"},
        {status.Error(codes.Unavailable,"EMBEDDING_UNAVAILABLE: gone"),503,"EMBEDDING_UNAVAILABLE"},
        {status.Error(codes.DeadlineExceeded,"EMBEDDING_TIMEOUT: slow"),504,"EMBEDDING_TIMEOUT"},
    }{
        client.err=test.err;req:=httptest.NewRequest("POST","/api/deployments/"+id+"/embeddings",strings.NewReader(body));req.Header.Set("Content-Type","application/json")
        response,err=app.Test(req);if err!=nil{t.Fatal(err)}
        var result map[string]any;json.NewDecoder(response.Body).Decode(&result);response.Body.Close()
        if response.StatusCode!=test.code || (test.reason!="" && result["code"]!=test.reason) || client.request.DeploymentId!=id || client.request.Inputs[0].Id!="chunk1" {t.Fatal(response.StatusCode,result,client.request)}
    }
}
