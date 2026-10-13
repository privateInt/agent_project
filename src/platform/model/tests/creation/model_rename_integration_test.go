package creation_test

import (
    "fmt"
    "reflect"
    "testing"
    "github.com/google/uuid"
)

func TestRenameLabelsPreservesArtifactsAndExecutionReferences(t *testing.T) {
    f:=newFixture(t)
    path:="/api/models/"+f.modelID
    _,before:=f.request(t,"GET","/api/model-versions/"+f.versionID,"")
    body:=fmt.Sprintf(`{"name":"한국어 등록 이름","expected_name":%q}`,"source-"+f.modelID)
    for i:=0;i<2;i++ { code,out:=f.request(t,"PATCH",path,body);if code!=200 || out["name"]!="한국어 등록 이름" || out["model_id"]!=f.modelID { t.Fatal(code,out) } }
    versionPath:="/api/model-versions/"+f.versionID
    versionBody:=`{"version":"검색 모델 v2","expected_version":"v1"}`
    for i:=0;i<2;i++ { code,out:=f.request(t,"PATCH",versionPath,versionBody);if code!=200 || out["version"]!="검색 모델 v2" || out["model_version_id"]!=f.versionID { t.Fatal(code,out) } }
    _,after:=f.request(t,"GET",versionPath,"")
    before["version"]="검색 모델 v2"
    if !reflect.DeepEqual(before,after) { t.Fatal("rename changed source or artifacts",before,after) }
    // A deployment created against the same UUID remains usable after relabeling.
    code,out:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"renamed-deployment")
    if code!=202 { t.Fatal("renamed model cannot deploy",code,out) }
    code,dep:=f.request(t,"GET","/api/deployments/"+out["deployment_id"].(string),"")
    if code!=200 || dep["model_version_id"]!=f.versionID { t.Fatal("version identity lost",code,dep) }
    for _,tc:=range []struct{path,body string;code int}{
        {path,`{"name":"stale edit","expected_name":"old"}`,409},
        {versionPath,`{"version":"stale edit","expected_version":"v1"}`,409},
        {path,`{"name":" ","expected_name":"한국어 등록 이름"}`,400},
        {path,`{"name":"missing expected"}`,400},
        {path,`{"name":"valid","expected_name":"x","artifact_uri":"spoof"}`,400},
        {"/api/models/"+uuid.NewString(),`{"name":"valid","expected_name":"old"}`,404},
    } { if code,out:=f.request(t,"PATCH",tc.path,tc.body);code!=tc.code { t.Fatal(tc,code,out) } }
    if code,_:=f.request(t,"DELETE",path,"");code!=200 { t.Fatal(code) }
    if code,_:=f.request(t,"PATCH",versionPath,versionBody);code!=409 { t.Fatal("removed model renamed",code) }
}
func TestRenameRejectsDuplicateLabels(t *testing.T) {
    f:=newFixture(t)
    execSQL(t,f.management,`INSERT INTO models(model_id,name,model_type) VALUES($1,'occupied','llm')`,uuid.NewString())
    execSQL(t,f.management,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type) VALUES($1,$2,'occupied','ready','huggingface')`,uuid.NewString(),f.modelID)
    code,out:=f.request(t,"PATCH","/api/models/"+f.modelID,fmt.Sprintf(`{"name":"occupied","expected_name":%q}`,"source-"+f.modelID))
    if code!=409 { t.Fatal(code,out) }
    code,out=f.request(t,"PATCH","/api/model-versions/"+f.versionID,`{"version":"occupied","expected_version":"v1"}`)
    if code!=409 { t.Fatal(code,out) }
    _,model:=f.request(t,"GET","/api/models/"+f.modelID,"")
    if model["name"]!="source-"+f.modelID { t.Fatal("failed rename changed model",model) }
}
