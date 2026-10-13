package searchstore
import("context";"encoding/json";"io";"net/http";"net/http/httptest";"strings";"testing";"knowledge-platform/internal/indexing";embeddingv1 "knowledge-platform/gen/model/embedding/v1")
func testVersion()indexing.Version{return indexing.Version{ID:"11111111-1111-4111-8111-111111111111",Count:2,Profile:&embeddingv1.EmbeddingProfile{Dimension:1024,EmbeddingSpaceId:"es_example"}}}
func TestBulkChecksEveryItemAndRequiresAlias(t *testing.T){
 v:=testVersion();name,alias,_:=names(v.ID);mode:="valid";calls:=0
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){calls++;if r.Method!="POST"||r.URL.Path!="/"+alias+"/_bulk"||r.URL.Query().Get("require_alias")!="true"||r.URL.Query().Get("wait_for_active_shards")!="all"||r.Header.Get("Content-Type")!="application/x-ndjson"{t.Error("write guard missing")};body,_:=io.ReadAll(r.Body);lines:=strings.Split(strings.TrimSpace(string(body)),"\n");if len(lines)!=4||!strings.HasSuffix(string(body),"\n"){t.Error("invalid NDJSON");http.Error(w,"invalid NDJSON",400);return}
 // Contract observed on the user's OpenSearch: _require_alias is rejected.
 for i,id:=range []string{"a","b"}{var action map[string]map[string]any;if json.Unmarshal([]byte(lines[2*i]),&action)!=nil{t.Error("invalid action");http.Error(w,"invalid action",400);return};meta:=action["index"];if len(action)!=1||len(meta)!=3||meta["_index"]!=alias||meta["_id"]!=id||meta["require_alias"]!=true{t.Error("invalid OpenSearch action metadata");w.WriteHeader(400);_,_=io.WriteString(w,unknownAliasResponse);return}}
 items:=[]any{};for _,id:=range []string{"a","b"}{items=append(items,map[string]any{"index":map[string]any{"_id":id,"_index":name,"status":201,"_shards":map[string]int{"total":2,"successful":2,"failed":0}}})}
 out:=map[string]any{"errors":false,"items":items};switch mode{case "short":out["items"]=items[:1];case "partial":out["errors"]=true;case "wrong":items[1].(map[string]any)["index"].(map[string]any)["_id"]="a";case "conflict":items[0].(map[string]any)["index"].(map[string]any)["status"]=409;case "missing_errors":delete(out,"errors")};_ = json.NewEncoder(w).Encode(out)
 }));defer server.Close();c:=&Client{Base:server.URL,HTTP:server.Client()};records:=[]indexing.Record{{ID:"a"},{ID:"b"}}
 if e:=c.Bulk(context.Background(),v,records);e!=nil{t.Fatal(e)};for _,bad:=range []string{"short","partial","wrong","conflict","missing_errors"}{mode=bad;if c.Bulk(context.Background(),v,records)==nil{t.Fatal("bad bulk accepted",bad)}};if calls!=6{t.Fatal("hidden retries",calls)}
}
func TestOwnershipAndDelete404(t *testing.T){
 v:=testVersion();name,alias,_:=names(v.ID);mode:="owned";deletes:=0
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){if r.Method=="DELETE"{deletes++;mode="missing";_ = json.NewEncoder(w).Encode(map[string]any{"acknowledged":true});return};if mode=="missing"{w.WriteHeader(404);return};mapping:=Mapping(v);if mode=="foreign"{mapping["_meta"]=map[string]any{"owner":"another-platform"}};_ = json.NewEncoder(w).Encode(map[string]any{name:map[string]any{"mappings":mapping,"aliases":map[string]any{alias:map[string]any{"is_write_index":true}},"settings":map[string]any{"index":map[string]any{"knn":"true"}}}})}));defer server.Close();c:=&Client{Base:server.URL,HTTP:server.Client()}
 if e:=c.Ensure(context.Background(),v);e!=nil{t.Fatal(e)};mode="foreign";if c.Delete(context.Background(),v)==nil||deletes!=0{t.Fatal("foreign index deleted")};mode="owned";if e:=c.Delete(context.Background(),v);e!=nil{t.Fatal(e)};if e:=c.Delete(context.Background(),v);e!=nil||deletes!=1{t.Fatal("delete retry not idempotent",e)}
}
func TestSealRejectsIncompleteCountAndShardFailures(t *testing.T){
 v:=testVersion();name,alias,_:=names(v.ID);bad:=true
 server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){var out any;switch{case strings.HasSuffix(r.URL.Path,"/_block/write"):out=map[string]any{"acknowledged":true,"shards_acknowledged":true,"indices":[]any{map[string]any{"name":name,"blocked":true}}};case strings.HasSuffix(r.URL.Path,"/_refresh"):out=map[string]any{"_shards":map[string]int{"total":2,"successful":2,"failed":0}};case strings.HasSuffix(r.URL.Path,"/_count"):count:=2;if bad{count=1};out=map[string]any{"count":count,"_shards":map[string]int{"total":1,"successful":1,"failed":0}};default:out=map[string]any{name:map[string]any{"mappings":Mapping(v),"aliases":map[string]any{alias:map[string]any{}},"settings":map[string]any{"index":map[string]string{"knn":"true"}}}}};_ = json.NewEncoder(w).Encode(out)}));defer server.Close();c:=&Client{Base:server.URL,HTTP:server.Client()}
 if c.Seal(context.Background(),v)==nil{t.Fatal("incomplete count sealed")};bad=false;if e:=c.Seal(context.Background(),v);e!=nil{t.Fatal(e)}
}
func TestConfigurationRejectsPlaintextAndURLCredentials(t *testing.T){for _,endpoint:=range []string{"http://search:9200","https://user:secret@search:9200","https://search:9200/path","https://search:9200?x=y"}{if _,e:=New(endpoint,"u","p","");e==nil{t.Fatal("unsafe endpoint accepted")}}}

// Compact response from the user's no-document parser probe, 2026-09-21.
const unknownAliasResponse=`{"error":{"root_cause":[{"type":"illegal_argument_exception","reason":"Action/metadata line [1] contains an unknown parameter [_require_alias]"}],"type":"illegal_argument_exception","reason":"Action/metadata line [1] contains an unknown parameter [_require_alias]"},"status":400}`
func TestHTTPErrorKeepsSafeDiagnosticIdentifiers(t *testing.T){
 for _,tt:=range []struct{name,body,want string}{
  {"parser",unknownAliasResponse,"OPENSEARCH_HTTP_400: illegal_argument_exception; unknown parameter [_require_alias]"},
  {"root cause",`{"error":{"root_cause":[{"type":"illegal_argument_exception","reason":"Action/metadata line [1] contains an unknown parameter [bad_field]"}]}}`,"OPENSEARCH_HTTP_400: illegal_argument_exception; unknown parameter [bad_field]"},
  {"private reason",`{"error":{"type":"mapper_parsing_exception","reason":"private document text and vector [0.123]"}}`,"OPENSEARCH_HTTP_400: mapper_parsing_exception"},
  {"non json","<html>private upstream response</html>","OPENSEARCH_HTTP_400"},
  {"string error",`{"error":"private upstream response"}`,"OPENSEARCH_HTTP_400"},
  {"invalid identifiers",`{"error":{"type":"bad\nvalue","reason":"Action/metadata line [1] contains an unknown parameter [private text]"}}`,"OPENSEARCH_HTTP_400"},
  {"long identifiers",`{"error":{"type":"`+strings.Repeat("x",81)+`","reason":"contains an unknown parameter [`+strings.Repeat("x",81)+`]"}}`,"OPENSEARCH_HTTP_400"},
 }{t.Run(tt.name,func(t *testing.T){
  server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){w.WriteHeader(400);_,_=io.WriteString(w,tt.body)}));defer server.Close();c:=&Client{Base:server.URL,HTTP:server.Client()}
  e:=c.request(context.Background(),"POST","/_bulk",nil,nil);h,ok:=e.(*HTTPError);if !ok||h.Code!=400||e.Error()!=tt.want{t.Fatalf("unexpected diagnostic: %v",e)}
 })}
}
