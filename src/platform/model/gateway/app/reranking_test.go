package app
import (
    "context"
    "encoding/json"
    "net/http/httptest"
    "strings"
    "testing"
    "model-platform/gateway/grpcclient"
    rerankingv1 "model-platform/gen/reranking/v1"
    "github.com/gofiber/fiber/v3"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)
type rerankingClient struct {rerankingv1.RerankingServiceClient;request *rerankingv1.RerankRequest;err error;calls int}
func(c *rerankingClient)GetProfile(_ context.Context,r *rerankingv1.GetProfileRequest,_ ...grpc.CallOption)(*rerankingv1.RerankerProfile,error){c.calls++;return &rerankingv1.RerankerProfile{DeploymentId:r.DeploymentId,Capability:"reranker",ScoreKind:"logit"},c.err}
func(c *rerankingClient)Rerank(_ context.Context,r *rerankingv1.RerankRequest,_ ...grpc.CallOption)(*rerankingv1.RerankResponse,error){c.request=r;c.calls++;return &rerankingv1.RerankResponse{Data:[]*rerankingv1.Score{{Id:r.Candidates[0].Id,Score:0}}},c.err}
func rerankBody()string{return `{"expected_reranker_profile_id":"rp_`+strings.Repeat("a",64)+`","query":" 서울? ","candidates":[{"id":"a","text":"서울이다"}]}`}
func TestRerankGatewayRejectsInvalidInputBeforeRPC(t *testing.T){
    good:=rerankBody();r,e:=parseRerank([]byte(good));if e!=nil||r.Query!=" 서울? "{t.Fatal(r,e)}
    id:="44247297-5184-4a36-9225-7791c80d3add";client:=&rerankingClient{};a:=fiber.New();New(&grpcclient.Clients{Reranking:client}).Register(a)
    for _,raw:=range []string{
        strings.Replace(good,`"query":" 서울? "`,`"query":"서울?","query":"other"`,1),
        strings.Replace(good,`"query":" 서울? "`,`"query":null`,1),
        strings.Replace(good,`"text":"서울이다"`,`"text":"서울이다","url":"http://external"`,1),
        strings.Replace(good,`"id":"a"`,`"id":"a","id":"b"`,1),
        strings.Replace(good,`"query":" 서울? "`,`"query":"서울?","top_k":1`,1),
        strings.Replace(good,`[{"id":"a","text":"서울이다"}]`,`[]`,1),good+` {}`,
    }{
        req:=httptest.NewRequest("POST","/api/deployments/"+id+"/rerank",strings.NewReader(raw));req.Header.Set("Content-Type","application/json")
        res,e:=a.Test(req);if e!=nil{t.Fatal(e)};res.Body.Close();if res.StatusCode!=400||client.calls!=0{t.Fatal("invalid request reached RPC",res.StatusCode)}
    }
}
func TestRerankGatewayMapsErrorsAndEmitsZeroScore(t *testing.T){
    id:="44247297-5184-4a36-9225-7791c80d3add";client:=&rerankingClient{};a:=fiber.New();New(&grpcclient.Clients{Reranking:client}).Register(a)
    for _,test:=range []struct{code codes.Code;http int}{{codes.OK,200},{codes.FailedPrecondition,409},{codes.DataLoss,502},{codes.Unavailable,503},{codes.DeadlineExceeded,504}}{
        client.err=nil;if test.code!=codes.OK{client.err=status.Error(test.code,"RERANK_TEST: unavailable")}
        req:=httptest.NewRequest("POST","/api/deployments/"+id+"/rerank",strings.NewReader(rerankBody()));req.Header.Set("Content-Type","application/json")
        res,e:=a.Test(req);if e!=nil{t.Fatal(e)};var data map[string]any;json.NewDecoder(res.Body).Decode(&data);res.Body.Close()
        if res.StatusCode!=test.http||client.request.DeploymentId!=id||client.request.Query!=" 서울? "{t.Fatal(res.StatusCode,data)}
        if test.code==codes.OK{row:=data["data"].([]any)[0].(map[string]any);if row["score"]!=float64(0)||row["index"]!=float64(0){t.Fatal("valid zero omitted",row)}}
    }
    client.err=nil;res,e:=a.Test(httptest.NewRequest("GET","/api/deployments/"+id+"/reranker-profile",nil));if e!=nil{t.Fatal(e)};res.Body.Close();if res.StatusCode!=200{t.Fatal(res.StatusCode)}
}
