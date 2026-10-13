package runtime

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "os"
    "path/filepath"
    "strings"
    "testing"

    deploymentv1 "model-platform/gen/deployment/v1"
    "model-platform/internal/execution"
    "github.com/google/uuid"
)

func TestServingSpec(t *testing.T) {
    demo,err:=Normalize(nil);if err!=nil || demo.Engine!="demo" {t.Fatal(demo,err)}
    actual,err:=Normalize(&deploymentv1.ServingSpec{Engine:" vLLM "});if err!=nil || actual.MaxModelLen!=2048 || actual.GPUMemoryUtilization!=0.5 {t.Fatal(actual,err)}
    for _,req:=range []*deploymentv1.ServingSpec{{Engine:"other"},{Engine:"demo",MaxModelLen:512},{Engine:"vllm",MaxModelLen:1},{Engine:"vllm",GpuMemoryUtilization:1}} {if _,err:=Normalize(req);err==nil {t.Fatal("invalid spec accepted")}}
    cfg:=Config{Network:"ops-infra",Volume:"cache",CacheDir:t.TempDir()}
    for _,image:=range []string{"vllm/vllm-openai:v0.15.0","registry:5000/vllm:v0.15.0","vllm/vllm-openai@sha256:"+strings.Repeat("a",64)} {cfg.Image=image;if err:=cfg.Validate();err!=nil{t.Fatal(image,err)}}
    for _,image:=range []string{"vllm/vllm-openai","registry:5000/vllm","vllm/vllm-openai:latest","vllm/vllm-openai:"} {cfg.Image=image;if cfg.Validate()==nil{t.Fatal("unpinned version accepted",image)}}
}
func testPlan() Plan {
    return Plan{DeploymentID:uuid.NewString(),OperationID:uuid.NewString(),ImageID:"sha256:"+strings.Repeat("a",64),Spec:Spec{Engine:"vllm",MaxModelLen:2048,GPUMemoryUtilization:0.5},Resources:execution.Resources{GPUCount:1,GPUIds:[]int32{0}}}
}
func TestDockerRuntimeIsolation(t *testing.T) {
    p:=testPlan();cfg:=Config{Network:"ops-infra",Volume:"test-cache"}
    args,err:=CreateArgs(cfg,p);if err!=nil {t.Fatal(err)}
    joined:=strings.Join(args," ")
    for _,expected:=range []string{"--pull never","--restart no","127.0.0.1::8000","--gpus device=0","--mount type=volume,src=test-cache,dst=/models,readonly","--model /models/"+p.DeploymentID+"/model","--served-model-name "+p.ModelName(),"--enforce-eager","HF_HUB_OFFLINE=1"} {if !strings.Contains(joined,expected) {t.Fatal("missing runtime contract",expected)}}
    for _,forbidden:=range []string{"--privileged","--ipc=host","docker.sock","MINIO_SECRET","HF_TOKEN","--trust-remote-code","--rm"} {if strings.Contains(joined,forbidden) {t.Fatal("unexpected runtime privilege",forbidden)}}
    p.Resources.GPUIds=nil;if _,err:=CreateArgs(cfg,p);err==nil {t.Fatal("implicit GPU placement accepted")}
}
func evidence(p Plan,c Config) *Evidence {
    e:=&Evidence{ID:strings.Repeat("b",64),Name:"/"+p.Name(),Image:p.ImageID}
    e.Config.Labels=map[string]string{"model-platform.owner":"deployment-serving","model-platform.deployment_id":p.DeploymentID,"model-platform.operation_id":p.OperationID,"model-platform.serving_plan":p.Hash()}
    e.HostConfig.RestartPolicy.Name="no"
    e.NetworkSettings.Networks=map[string]json.RawMessage{c.Network:json.RawMessage(`{}`)}
    e.Mounts=append(e.Mounts,struct{Type,Name,Destination string;RW bool}{"volume",c.Volume,"/models",false})
    return e
}
func TestForeignRuntimeAndPortAreRejected(t *testing.T) {
    p:=testPlan();cfg:=Config{Network:"ops-infra",Volume:"test-cache"};e:=evidence(p,cfg)
    if err:=validateEvidence(p,cfg,e);err!=nil {t.Fatal(err)}
    e.Config.Labels["model-platform.owner"]="someone-else";if validateEvidence(p,cfg,e)==nil {t.Fatal("foreign owner accepted")}
    e=evidence(p,cfg);e.Image="sha256:"+strings.Repeat("c",64);if validateEvidence(p,cfg,e)==nil {t.Fatal("changed image accepted")}
    if _,err:=Endpoint(e);err==nil {t.Fatal("missing loopback binding accepted")}
    // Decode Docker's real PortBinding field casing.
    if err:=json.Unmarshal([]byte(`{"NetworkSettings":{"Ports":{"8000/tcp":[{"HostIp":"127.0.0.1","HostPort":"18000"}]}}}`),e);err!=nil {t.Fatal(err)}
    if url,err:=Endpoint(e);err!=nil || url!="http://127.0.0.1:18000/v1" {t.Fatal(url,err)}
    json.Unmarshal([]byte(`{"NetworkSettings":{"Ports":{"8000/tcp":[{"HostIp":"0.0.0.0","HostPort":"18000"}]}}}`),e)
    if _,err:=Endpoint(e);err==nil {t.Fatal("public binding accepted")}
}
func hash(data []byte)string {h:=sha256.Sum256(data);return hex.EncodeToString(h[:])}
func TestSnapshotIntegrityAndPaths(t *testing.T) {
    config,weights:=[]byte(`{"architectures":["Gemma3ForCausalLM"]}`),[]byte("fixture-weights")
    m:=snapshotManifest{Files:[]snapshotFile{{"config.json",hash(config),int64(len(config))},{"model.safetensors",hash(weights),int64(len(weights))}}}
    data,_:=json.Marshal(m);source:=Source{SHA256:hash(data),SizeBytes:int64(len(config)+len(weights))}
    if _,err:=parseSnapshot(data,source);err!=nil {t.Fatal(err)}
    dir:=t.TempDir();os.WriteFile(filepath.Join(dir,"_manifest.json"),data,0o644);os.WriteFile(filepath.Join(dir,"config.json"),config,0o644);os.WriteFile(filepath.Join(dir,"model.safetensors"),weights,0o644)
    if err:=validateDirectory(dir,source);err!=nil {t.Fatal(err)}
    os.WriteFile(filepath.Join(dir,"model.safetensors"),[]byte("changed-weights"),0o644)
    if validateDirectory(dir,source)==nil {t.Fatal("corrupt cache accepted")}
    for _,name:=range []string{"../escape","/absolute","a/../b","a\\b","_manifest.json","config.json"} {
        bad:=m;bad.Files=append([]snapshotFile(nil),m.Files...);bad.Files[1].Path=name
        raw,_:=json.Marshal(bad);expected:=source;expected.SHA256=hash(raw)
        if _,err:=parseSnapshot(raw,expected);err==nil {t.Fatal("unsafe manifest accepted",name)}
    }
    source.SHA256=strings.Repeat("0",64);if _,err:=parseSnapshot(data,source);err==nil {t.Fatal("unauthenticated manifest accepted")}
}
