package creation_test

import (
    "context"
    "fmt"
    "net/url"
    "strings"
    "testing"
    "time"
    operationv1 "model-platform/gen/operation/v1"
    "github.com/google/uuid"
    "github.com/jackc/pgx/v5/pgxpool"
)

func listItems(t *testing.T, f *fixture, path string) ([]any,string) {
    t.Helper();code,body:=f.request(t,"GET",path,"")
    if code!=200 {t.Fatalf("list %s: %d %v",path,code,body)}
    items,ok:=body["items"].([]any);if !ok{t.Fatalf("items not an array: %v",body)}
    token,ok:=body["next_cursor"].(string);if !ok{t.Fatal("missing cursor",body)}
    return items,token
}

// Exercise the real REST -> gRPC -> PostgreSQL path for every new collection.
// Ties, concurrent newer inserts, and deletion of the cursor's row must not
// shift the remaining page or require that cursor row to still exist.
func TestListKeysetAcrossCollections(t *testing.T){
    for _,kind:=range []string{"models","model-versions","training-runs","deployments","operations"}{t.Run(kind,func(t *testing.T){
        f:=newFixture(t)
        execSQL(t,f.management,`UPDATE model_versions SET created_at='2000-01-01'`)
        var pool *pgxpool.Pool;var table,idField,path string
        fixed:=time.Date(2026,9,9,8,0,0,123456000,time.UTC)
        insert:=func(n int,newer bool){
            id:=fmt.Sprintf("10000000-0000-0000-0000-%012d",n);created:=fixed;if newer{created=created.Add(time.Minute)}
            switch kind {
            case "models":
                pool=f.management;table="models";idField="model_id";path="/api/models?q=catalog-"
                execSQL(t,pool,`INSERT INTO models(model_id,name,model_type,created_at) VALUES($1,$2,'llm',$3)`,id,fmt.Sprintf("catalog-%d",n),created)
            case "model-versions":
                pool=f.management;table="model_versions";idField="model_version_id";path="/api/model-versions?model_id="+f.modelID+"&q=page-"
                execSQL(t,pool,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind,created_at) VALUES($1,$2,$3,'importing','huggingface','snapshot',$4)`,id,f.modelID,fmt.Sprintf("page-%d",n),created)
            case "training-runs":
                pool=f.training;table="training_runs";idField="training_run_id";path="/api/training-runs?source_model_version_id="+f.versionID
                execSQL(t,pool,`INSERT INTO training_runs(training_run_id,source_model_version_id,status,created_at) VALUES($1,$2,'created',$3)`,id,f.versionID,created)
            case "deployments":
                pool=f.deployment;table="deployments";idField="deployment_id";path="/api/deployments?model_version_id="+f.versionID
                execSQL(t,pool,`INSERT INTO deployments(deployment_id,model_version_id,status,created_at) VALUES($1,$2,'created',$3)`,id,f.versionID,created)
            case "operations":
                pool=f.ops;table="operations";idField="operation_id";path="/api/operations?resource_type=training_run"
                execSQL(t,pool,`INSERT INTO operations(operation_id,kind,execution_type,resource_type,resource_id,status,phase,created_at) VALUES($1,'training','training.run','training_run',$2,'pending','queued',$3)`,id,f.versionID,created)
            }
        }
        for i:=1;i<=3;i++{insert(i,false)}
        first,token:=listItems(t,f,path+"&limit=1")
        if len(first)!=1 || first[0].(map[string]any)[idField]!="10000000-0000-0000-0000-000000000003" || token==""{t.Fatal(first,token)}
        if first[0].(map[string]any)["created_at"]!="2026-09-09T08:00:00.123456Z"{t.Fatal("timestamp precision lost",first)}
        insert(4,true)
        execSQL(t,pool,"DELETE FROM "+table+" WHERE "+idField+"=$1","10000000-0000-0000-0000-000000000003")
        second,end:=listItems(t,f,path+"&limit=2&cursor="+url.QueryEscape(token))
        if len(second)!=2 || end!="" {t.Fatal("wrong remaining page",second,end)}
        for i,item:=range second {if item.(map[string]any)[idField]!=fmt.Sprintf("10000000-0000-0000-0000-%012d",2-i){t.Fatal("duplicate or missing item",second)}}
        fresh,_:=listItems(t,f,path+"&limit=1")
        if fresh[0].(map[string]any)[idField]!="10000000-0000-0000-0000-000000000004"{t.Fatal("refresh omitted new row")}
        if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=0 {t.Fatal("browsing enqueued a job")}
    })}
}

func TestModelSearchLiteralTextAndVersionFilters(t *testing.T){
    f:=newFixture(t)
    for _,name:=range []string{"한글 Gemma 100%_model", "한글 Gemma 100XXmodel"}{
        code,res:=f.request(t,"POST","/api/models",fmt.Sprintf(`{"name":%q,"model_type":"llm"}`,name));if code!=201{t.Fatal(res)}
    }
    items,_:=listItems(t,f,"/api/models?q="+url.QueryEscape("100%_"))
    if len(items)!=1 || items[0].(map[string]any)["name"]!="한글 Gemma 100%_model"{t.Fatal("wildcards broadened literal search",items)}
    items,_=listItems(t,f,"/api/models?q="+url.QueryEscape("한글 gemma")+"&model_type=LLM")
    if len(items)!=2{t.Fatal("unicode/case-insensitive search failed",items)}
    items,_=listItems(t,f,"/api/models?q="+url.QueryEscape("' OR true --"))
    if len(items)!=0{t.Fatal("SQL text changed search meaning",items)}
    // The fixture version is ready, and the owner label is projected in one DB query.
    for _,path:=range []string{"/api/model-versions?model_id="+f.modelID,"/api/models/"+f.modelID+"/versions?state=ready&artifact_kind=snapshot&source_type=huggingface"}{
        items,_=listItems(t,f,path)
        if len(items)!=1 || items[0].(map[string]any)["model_name"]!="source-"+f.modelID || items[0].(map[string]any)["model_version_id"]!=f.versionID{t.Fatal(items)}
    }
    items,_=listItems(t,f,"/api/model-versions?model_id="+f.modelID+"&artifact_kind=adapter")
    if len(items)!=0{t.Fatal("artifact_kind filter ignored",items)}
    items,_=listItems(t,f,"/api/models/"+uuid.NewString()+"/versions")
    if len(items)!=0{t.Fatal("unknown parent leaked unrelated versions")}
    _,cursor:=listItems(t,f,"/api/models?q="+url.QueryEscape("한글 gemma")+"&limit=1")
    for _,path:=range []string{"/api/models?q=different&cursor=","/api/model-versions?cursor="}{
        if code,res:=f.request(t,"GET",path+url.QueryEscape(cursor),"");code!=400{t.Fatal("cursor accepted different scope",code,res)}
    }
}

func TestListRelatedStateFiltersAndPublicSummaries(t *testing.T){
    f:=newFixture(t);runID,otherID,datasetID:=uuid.NewString(),uuid.NewString(),uuid.NewString()
    for _,id:=range []string{runID,otherID}{
        state:="failed";dataset:="{}";if id==runID{state="succeeded";dataset=fmt.Sprintf(`{"source_type":"version","format":"chat_messages","dataset_version_id":%q}`,datasetID)}
        execSQL(t,f.training,`INSERT INTO training_runs(training_run_id,source_model_version_id,status,dataset_spec,result_json) VALUES($1,$2,$3,$4::jsonb,'large result')`,id,f.versionID,state,dataset)
    }
    items,_:=listItems(t,f,"/api/training-runs?status=succeeded&source_model_version_id="+f.versionID+"&dataset_version_id="+datasetID)
    if len(items)!=1 || items[0].(map[string]any)["training_run_id"]!=runID{t.Fatal(items)}
    if _,ok:=items[0].(map[string]any)["result_json"];ok{t.Fatal("list returned a large training result")}
    depID:=uuid.NewString()
    execSQL(t,f.deployment,`INSERT INTO deployments(deployment_id,model_version_id,status,engine,runtime_state,desired_state,runtime_endpoint,apply_status) VALUES($1,$2,'stopped','vllm-lora','stopped','stopped','http://stale/v1','ready')`,depID,f.versionID)
    items,_=listItems(t,f,"/api/deployments?status=stopped&runtime_state=stopped&model_version_id="+f.versionID)
    if len(items)!=1{t.Fatal(items)}
    d:=items[0].(map[string]any)
    if d["endpoint"]!="" || d["apply_status"]!="ready" || d["artifact_kind"]!="adapter" || d["serving"].(map[string]any)["engine"]!="vllm" {t.Fatal(items)}
    opID:=uuid.NewString()
    execSQL(t,f.ops,`INSERT INTO operations(operation_id,kind,execution_type,resource_type,resource_id,status,phase,error_code,dispatch_token,dispatch_owner,execution_instance_id) VALUES($1,'training','training.run','training_run',$2,'pending','waiting_resources','GPU_BUSY',$3,'private-owner',$4)`,opID,runID,uuid.NewString(),uuid.NewString())
    items,_=listItems(t,f,"/api/operations?status=pending&resource_type=training_run&resource_id="+runID+"&kind=training&phase=waiting_resources")
    if len(items)!=1 || items[0].(map[string]any)["error_code"]!="GPU_BUSY"{t.Fatal(items)}
    for _,key:=range []string{"dispatch_token","dispatch_owner","execution_instance_id"}{if _,ok:=items[0].(map[string]any)[key];ok{t.Fatal("REST grant leak")}}
    rpc,err:=f.clients.Operation.ListOperations(context.Background(),&operationv1.ListOperationsRequest{ResourceId:runID});if err!=nil{t.Fatal(err)}
    if len(rpc.Operations)!=1 || rpc.Operations[0].DispatchToken!="" || rpc.Operations[0].DispatchOwner!="" || rpc.Operations[0].ExecutionInstanceId!=""{t.Fatal("gRPC list leaked execution grant")}
}

func TestListValidationAndDatasetSearchCompatibility(t *testing.T){
    f:=newFixture(t)
    for _,path:=range []string{
        "/api/models?model_type=wrong", "/api/models?q="+strings.Repeat("a",201),"/api/models?cursor=invalid",
        "/api/model-versions?model_id=bad", "/api/model-versions?state=wrong", "/api/model-versions?source_type=wrong",
        "/api/training-runs?dataset_version_id=bad", "/api/training-runs?status=wrong",
        "/api/deployments?model_version_id=bad", "/api/deployments?runtime_state=wrong",
        "/api/operations?resource_type=wrong", "/api/operations?resource_id=bad",
        "/api/models/"+f.modelID+"/versions?model_id="+f.modelID,
    } {t.Run(path,func(t *testing.T){if code,res:=f.request(t,"GET",path,"");code!=400{t.Fatal(code,res)}})}
    for i:=0;i<3;i++ {
        code,res:=f.request(t,"POST","/api/datasets",fmt.Sprintf(`{"name":"검색_자료 %d"}`,i));if code!=201{t.Fatal(res)}
    }
    search:="/api/datasets?q="+url.QueryEscape("검색_자료")+"&limit=2"
    code,first:=f.request(t,"GET",search,"");if code!=200{t.Fatal(first)}
    if len(first["datasets"].([]any))!=2 || first["next_after_id"]==""{t.Fatal(first)}
    code,last:=f.request(t,"GET",search+"&after_id="+first["next_after_id"].(string),"")
    if code!=200 || len(last["datasets"].([]any))!=1 || last["next_after_id"]!=""{t.Fatal(last)}
    code,empty:=f.request(t,"GET","/api/datasets?q="+url.QueryEscape("검색X자료"),"")
    if code!=200 || len(empty["datasets"].([]any))!=0{t.Fatal("dataset wildcard semantics changed",empty)}
}
