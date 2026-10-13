package app

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	indexingv1 "knowledge-platform/gen/indexing/v1"
	"knowledge-platform/internal/testsupport"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type searchClient struct { indexingv1.IndexingServiceClient; calls int; err error; request *indexingv1.SearchRequest }
func (c *searchClient) Search(_ context.Context,r *indexingv1.SearchRequest,_ ...grpc.CallOption)(*indexingv1.JSONResponse,error){c.calls++;c.request=r;if c.err!=nil{return nil,c.err};return &indexingv1.JSONResponse{Json:[]byte(`{"results":[]}`)},nil}
func TestSearchRESTPreservesRequestAndErrorStatus(t *testing.T){
	v:=testsupport.SearchVersion();vector:=make([]float64,1024);vector[0]=1
	body,_:=json.Marshal(map[string]any{"index_version_id":v.ID,"embedding_space_id":v.Profile.EmbeddingSpaceId,"query_embedding":vector,"filters":map[string]any{"canonical_ids":[]string{v.CanonicalIDs[0]}}})
	client:=&searchClient{};handler:=(&Server{Indexes:client}).Handler();path:="/api/indexes/"+v.IndexID+"/search"
	call:=func(path,body string)*httptest.ResponseRecorder{r:=httptest.NewRequest("POST",path,strings.NewReader(body));w:=httptest.NewRecorder();handler.ServeHTTP(w,r);return w}
	if got:=call(path,string(body));got.Code!=200||client.calls!=1||string(client.request.QueryJson)!=string(body)||client.request.IndexId!=v.IndexID{t.Fatal("request changed",got.Code)}
	for _,bad:=range []struct{path,body string}{{path+"?filters=all",string(body)},{path,`{"query_text":"infer"}`},{path,strings.Repeat(" ",65537)}}{if got:=call(bad.path,bad.body);got.Code!=400&&got.Code!=413{t.Fatal("invalid request accepted",got.Code)}}
	if client.calls!=1{t.Fatal("invalid query reached service")}
	for code,want:=range map[codes.Code]int{codes.InvalidArgument:400,codes.NotFound:404,codes.FailedPrecondition:409,codes.DataLoss:502,codes.Unavailable:503,codes.DeadlineExceeded:504,codes.ResourceExhausted:413}{client.err=status.Error(code,"SEARCH_TEST: bounded reason");if got:=call(path,string(body));got.Code!=want{t.Fatal(code,got.Code)}}
}

func TestConsoleModelProxyIsNarrowAndUsesOfficialAPI(t *testing.T){
	v:=testsupport.SearchVersion();calls:=0;var forwarded string
	upstream:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){calls++;if r.URL.Path!="/api/deployments/"+v.Profile.DeploymentId+"/embeddings"{t.Error("wrong Model path")};raw,_:=io.ReadAll(r.Body);forwarded=string(raw);w.Header().Set("Content-Type","application/json");_,_=io.WriteString(w,`{"role":"query"}`)}));defer upstream.Close()
	proxy,err:=NewModelConsole(upstream.URL);if err!=nil{t.Fatal(err)};handler:=(&Server{ModelConsole:proxy}).Handler()
	path:="/console/model/deployments/"+v.Profile.DeploymentId+"/embeddings"
	body:=`{"role":"query","expected_embedding_space_id":"`+v.Profile.EmbeddingSpaceId+`","inputs":[{"id":"query-1","text":"원문 질문"}]}`
	call:=func(path,body string)int{w:=httptest.NewRecorder();handler.ServeHTTP(w,httptest.NewRequest("POST",path,strings.NewReader(body)));return w.Code}
	if call(path,body)!=200||calls!=1||forwarded!=body{t.Fatal("query rewritten or endpoint changed")}
	for _,bad:=range []string{strings.Replace(body,`"query"`,`"document"`,1),strings.Replace(body,`"inputs":`,`"url":"http://other", "inputs":`,1),strings.Replace(body,`"text":"원문 질문"`,`"text":null`,1),strings.Replace(body,`"id":"query-1"`,`"id":"x","id":"y"`,1)}{if call(path,bad)!=400{t.Fatal("unscoped model request accepted")}}
	if call(path+"?url=http://other",body)!=400||calls!=1{t.Fatal("proxy accepted arbitrary query or inference")}
	for _,bad:=range []string{"http://user:secret@host","http://host/path","http://host?x=y","file:///tmp/model"}{if _,err:=NewModelConsole(bad);err==nil{t.Fatal("unscoped origin accepted")}}
}

func TestRerankSearchAndProfileReadRoutes(t *testing.T){
	v:=testsupport.SearchVersion();p:=testsupport.RerankerProfile();vector:=make([]float64,1024);vector[0]=1
	body,_:=json.Marshal(map[string]any{"index_version_id":v.ID,"embedding_space_id":v.Profile.EmbeddingSpaceId,"query_embedding":vector,"query_text":" 원문 질문 ","candidate_k":20,"rerank":map[string]any{"enabled":true,"deployment_id":p.DeploymentId,"expected_reranker_profile_id":p.RerankerProfileId}})
	client:=&searchClient{};handler:=(&Server{Indexes:client}).Handler();w:=httptest.NewRecorder()
	handler.ServeHTTP(w,httptest.NewRequest("POST","/api/indexes/"+v.IndexID+"/search",strings.NewReader(string(body))))
	if w.Code!=200||client.calls!=1||string(client.request.QueryJson)!=string(body){t.Fatal("rerank request changed",w.Code)}
	calls:=0;upstream:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){calls++;if r.Method!="GET"||r.URL.Path!="/api/deployments/"+p.DeploymentId+"/reranker-profile"{t.Error("wrong profile request")};_=json.NewEncoder(w).Encode(p)}));defer upstream.Close()
	proxy,e:=NewModelConsole(upstream.URL);if e!=nil{t.Fatal(e)};handler=(&Server{ModelConsole:proxy}).Handler()
	path:="/console/model/deployments/"+p.DeploymentId
	w=httptest.NewRecorder();handler.ServeHTTP(w,httptest.NewRequest("GET",path+"/reranker-profile",nil))
	if w.Code!=200||calls!=1{t.Fatal("profile read failed",w.Code)}
	for _,suffix:=range []string{"/rerank","/reranker-profile"}{w=httptest.NewRecorder();handler.ServeHTTP(w,httptest.NewRequest("POST",path+suffix,strings.NewReader(`{}`)));if w.Code<400||calls!=1{t.Fatal("browser inference route escaped",w.Code)}}
}
