package creation_test

import (
    "context"
    "reflect"
    "sync"
    "testing"
    "time"

    managementv1 "model-platform/gen/management/v1"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

func TestModelCatalogDeletionPreservesArtifactsHistoryAndReplay(t *testing.T) {
    f := newFixture(t)
    depID, runID := uuid.NewString(), uuid.NewString()
    execSQL(t,f.deployment,`INSERT INTO deployments(deployment_id,model_version_id,status,runtime_state,desired_state) VALUES($1,$2,'ready','ready','running')`,depID,f.versionID)
    execSQL(t,f.training,`INSERT INTO training_runs(training_run_id,source_model_version_id,status) VALUES($1,$2,'running')`,runID,f.versionID)
    _, before := f.request(t,"GET","/api/model-versions/"+f.versionID,"")
    path := "/api/models/"+f.modelID
    code, deleted := f.request(t,"DELETE",path,"")
    if code != 200 || deleted["model_id"] != f.modelID || deleted["deleted_at"] == nil { t.Fatal(code,deleted) }
    code, replay := f.request(t,"DELETE",path,"")
    if code != 200 || !reflect.DeepEqual(deleted,replay) { t.Fatal("delete replay changed",code,replay) }
    code, detail := f.request(t,"GET",path,"")
    if code != 200 || !reflect.DeepEqual(deleted,detail) { t.Fatal("history lookup lost",code,detail) }
    for _, path := range []string{"/api/models", "/api/model-versions", "/api/models/"+f.modelID+"/versions"} {
        rows,_ := listItems(t,f,path)
        if len(rows) != 0 { t.Fatal("deleted model remained in catalog",path,rows) }
    }
    _, after := f.request(t,"GET","/api/model-versions/"+f.versionID,"")
    if !reflect.DeepEqual(before,after) { t.Fatal("version or artifact changed",after) }
    if count(t,f.training,`SELECT count(*) FROM training_runs WHERE training_run_id=$1 AND status='running'`,runID)!=1 || count(t,f.deployment,`SELECT count(*) FROM deployments WHERE deployment_id=$1 AND desired_state='running'`,depID)!=1 { t.Fatal("deletion stopped existing work") }
    code, reply := f.request(t,"POST",path+"/imports/huggingface",`{"version":"new","repo_id":"fixture/model"}`)
    if code != 409 { t.Fatal("new import accepted deleted parent",code,reply) }
    if count(t,f.ops,`SELECT count(*) FROM operation_outbox`) != 0 { t.Fatal("deletion/import rejection enqueued a job") }
    if code,_:=f.request(t,"DELETE","/api/models/bad","");code!=400 { t.Fatal("invalid ID",code) }
    if code,_:=f.request(t,"DELETE","/api/models/"+uuid.NewString(),"");code!=404 { t.Fatal("missing ID",code) }
}

func TestModelCatalogDeletionKeepsAdmittedImportReplay(t *testing.T) {
    f := newFixture(t)
    path := "/api/models/"+f.modelID
    body := `{"version":"admitted","repo_id":"fixture/model"}`
    code, first := f.request(t,"POST",path+"/imports/huggingface",body,"before-delete")
    if code!=202 { t.Fatal(code,first) }
    if code,_:=f.request(t,"DELETE",path,"");code!=200 { t.Fatal(code) }
    code, again := f.request(t,"POST",path+"/imports/huggingface",body,"before-delete")
    if code!=202 || !reflect.DeepEqual(first,again) { t.Fatal("admitted request lost",code,again) }
    if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1 { t.Fatal("replay created another job") }
}

func TestModelCatalogDeleteAndImportSerialize(t *testing.T) {
    f := newFixture(t)
    // Exercise both orders: import holds the parent lock, then delete waits;
    // deleted parent then rejects a fresh import with no reservation or outbox.
    ctx,cancel:=context.WithTimeout(context.Background(),10*time.Second);defer cancel()
    tx,err:=f.management.Begin(ctx);if err!=nil { t.Fatal(err) }
    defer tx.Rollback(context.Background())
    if _,err=tx.Exec(context.Background(),`SELECT model_id FROM models WHERE model_id=$1 FOR UPDATE`,f.modelID);err!=nil { t.Fatal(err) }
    var wg sync.WaitGroup
    var deleteErr error
    wg.Add(1)
    go func(){ defer wg.Done(); _,deleteErr=f.clients.Management.DeleteModel(ctx,&managementv1.GetModelRequest{ModelId:f.modelID}) }()
    // A version admitted in this transaction survives subsequent catalog deletion.
    versionID:=uuid.NewString()
    if _,err=tx.Exec(context.Background(),`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind) VALUES($1,$2,'before-delete','importing','huggingface','snapshot')`,versionID,f.modelID);err!=nil { t.Fatal(err) }
    if err=tx.Commit(context.Background());err!=nil { t.Fatal(err) }
    wg.Wait();if deleteErr!=nil { t.Fatal(deleteErr) }
    if count(t,f.management,`SELECT count(*) FROM model_versions WHERE model_version_id=$1 AND state='importing'`,versionID)!=1 { t.Fatal("in-flight import removed") }
    _,err=f.clients.Management.ImportHuggingFace(context.Background(),&managementv1.ImportHuggingFaceRequest{ModelId:f.modelID,Version:"after-delete",RepoId:"fixture/model",IdempotencyKey:uuid.NewString()})
    if status.Code(err)!=codes.FailedPrecondition { t.Fatal("post-delete import accepted",err) }
}
