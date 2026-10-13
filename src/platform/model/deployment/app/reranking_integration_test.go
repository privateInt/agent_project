package app

import (
    "context"
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "io"
    "net/http"
    "os"
    "path/filepath"
    "sort"
    "strings"
    "testing"

    "model-platform/deployment/grpcclient"
    serving "model-platform/deployment/runtime"
    commonv1 "model-platform/gen/common/v1"
    deploymentv1 "model-platform/gen/deployment/v1"
    managementv1 "model-platform/gen/management/v1"
    rerankingv1 "model-platform/gen/reranking/v1"
    "model-platform/internal/testsupport"
    "github.com/google/uuid"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

type rerankerManagement struct {managementv1.ManagementServiceClient; version *managementv1.ModelVersion; model *managementv1.Model}
func(m *rerankerManagement)GetModelVersion(context.Context,*managementv1.GetModelVersionRequest,...grpc.CallOption)(*managementv1.ModelVersion,error){return m.version,nil}
func(m *rerankerManagement)GetModel(context.Context,*managementv1.GetModelRequest,...grpc.CallOption)(*managementv1.Model,error){return m.model,nil}
type rerankerTransport func(*http.Request)(*http.Response,error)
func(f rerankerTransport)RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
func rerankerDigest(raw []byte)string{hash:=sha256.Sum256(raw);return hex.EncodeToString(hash[:])}

func newRerankerFixture(t *testing.T)(*servingFixture,*rerankerManagement){
    t.Helper();ctx:=context.Background();pool:=testsupport.Postgres(t)
    if err:=InitSchema(ctx,pool);err!=nil{t.Fatal(err)}
    files:=map[string]string{
        "config.json":`{"model_type":"xlm-roberta","architectures":["XLMRobertaForSequenceClassification"],"hidden_size":1024,"max_position_embeddings":514,"vocab_size":250002,"id2label":{"0":"LABEL_0"},"bos_token_id":0,"eos_token_id":2,"pad_token_id":1}`,
        "tokenizer_config.json":`{"tokenizer_class":"XLMRobertaTokenizer","model_max_length":512,"cls_token":"<s>","sep_token":"</s>"}`,
        "tokenizer.json":`{"version":"1.0","truncation":{"direction":"Right","max_length":512,"strategy":"LongestFirst","stride":0},"padding":{"strategy":"BatchLongest","direction":"Right","pad_to_multiple_of":null,"pad_id":1,"pad_type_id":0,"pad_token":"<pad>"},"post_processor":{"type":"TemplateProcessing","single":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"pair":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"Sequence":{"id":"B","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"special_tokens":{"</s>":{"id":"</s>","ids":[2],"tokens":["</s>"]},"<s>":{"id":"<s>","ids":[0],"tokens":["<s>"]}}}}`,
        "model.safetensors":"synthetic weights; no GPU is loaded in this test",
    }
    var total int64;names:=[]string{};for name:=range files{names=append(names,name)};sort.Strings(names)
    entries:=[]map[string]any{}
    for _,name:=range names{body:=[]byte(files[name]);total+=int64(len(body));entries=append(entries,map[string]any{"path":name,"sha256":rerankerDigest(body),"size_bytes":len(body)})}
    manifest,_:=json.Marshal(map[string]any{"files":entries})
    mid,vid:=uuid.NewString(),uuid.NewString()
    mgmt:=&rerankerManagement{model:&managementv1.Model{ModelId:mid,ModelType:"llm"},version:&managementv1.ModelVersion{
        ModelId:mid,ModelVersionId:vid,State:"ready",SourceType:"huggingface",ArtifactKind:"snapshot",RepoId:serving.RerankerRepository,ResolvedRevision:strings.Repeat("c",40),
        ArtifactUri:"s3://model-artifacts/reranker-fixture/",ArtifactSha256:rerankerDigest(manifest),ArtifactSizeBytes:total,
    }}
    ops:=&servingOperations{};driver:=&runtimeDriver{};s:=New(pool,&grpcclient.Clients{Management:mgmt,Operation:ops});cache:=t.TempDir()
    fetch:=serving.FetchFunc(func(_ context.Context,p serving.Plan)error{
        dir:=filepath.Join(cache,p.DeploymentID,"model");if err:=os.MkdirAll(dir,0700);err!=nil{return err}
        for name,body:=range files{if err:=os.WriteFile(filepath.Join(dir,name),[]byte(body),0600);err!=nil{return err}}
        return os.WriteFile(filepath.Join(dir,"_manifest.json"),manifest,0600)
    })
    if err:=s.ConfigureServing(serving.Config{Image:"vllm/vllm-openai:v0.15.0",Network:"ops-infra",Volume:"test-cache",CacheDir:cache},driver,fetch);err!=nil{t.Fatal(err)}
    dep,err:=s.CreateServingDeployment(ctx,&deploymentv1.DeployModelRequest{ModelVersionId:vid,IdempotencyKey:uuid.NewString(),Serving:&deploymentv1.ServingSpec{Engine:"vllm",Profile:serving.RerankerProfile,MaxModelLen:512},Resources:&commonv1.ExecutionResources{GpuIds:[]int32{0}}})
    if err!=nil{t.Fatal(err)}
    f:=&servingFixture{s:s,driver:driver,ops:ops,sourceID:vid,request:&deploymentv1.StartServingRequest{DeploymentId:dep.DeploymentId,OperationId:dep.OperationId,DispatchToken:uuid.NewString(),ExecutionInstanceId:uuid.NewString()}}
    f.grant(t);driver.setHealthy(true);f.cycle(t);f.cycle(t)
    if f.view(t).GetStatus()!="ready"{t.Fatal("fixture did not become ready")}
    return f,mgmt
}

func TestRerankerProfileInferenceAndStopIntegration(t *testing.T){
    f,_:=newRerankerFixture(t);ctx:=context.Background();server:=&RerankingServer{Service:f.s}
    p,err:=server.GetProfile(ctx,&rerankingv1.GetProfileRequest{DeploymentId:f.request.DeploymentId});if err!=nil{t.Fatal(err)}
    if p.ProfileId!=serving.RerankerProfile||p.RegisteredModelType!="llm"||p.MaxInputTokens!=512{t.Fatal(p)}
    calls:=0;stopDuringScore:=false
    server.HTTP=&http.Client{Transport:rerankerTransport(func(r *http.Request)(*http.Response,error){
        calls++;if r.URL.Host!="model-serving-"+p.DeploymentId+":8000"{t.Fatal("unexpected runtime",r.URL)}
        body:=`{"count":2,"max_model_len":512,"tokens":[20,30]}`
        if r.URL.Path=="/score"{
            if stopDuringScore{f.stop(t)}
            body=`{"model":"`+p.ServedModelName+`","data":[{"index":1,"score":-2},{"index":0,"score":1}],"usage":{"prompt_tokens":16,"total_tokens":16}}`
        }else if r.URL.Path!="/tokenize"{t.Fatal("unexpected request",r.URL)}
        return &http.Response{StatusCode:200,Header:make(http.Header),Body:io.NopCloser(strings.NewReader(body))},nil
    })}
    req:=&rerankingv1.RerankRequest{DeploymentId:p.DeploymentId,ExpectedRerankerProfileId:"rp_"+strings.Repeat("0",64),Query:"서울?",Candidates:[]*rerankingv1.Candidate{{Id:"a",Text:"서울"},{Id:"b",Text:"사과"}}}
    if _,err=server.Rerank(ctx,req);status.Code(err)!=codes.FailedPrecondition||calls!=0{t.Fatal("mismatch reached inference",err,calls)}
    req.ExpectedRerankerProfileId=p.RerankerProfileId
    out,err:=server.Rerank(ctx,req);if err!=nil||len(out.GetData())!=2||out.Data[0].Id!="a"||out.Data[0].Score!=1||calls!=4{t.Fatal(out,err,calls)}
    stopDuringScore=true
    if _,err=server.Rerank(ctx,req);status.Code(err)!=codes.FailedPrecondition{t.Fatal("stopped deployment scores accepted",err)}
    before:=calls;if _,err=server.GetProfile(ctx,&rerankingv1.GetProfileRequest{DeploymentId:p.DeploymentId});status.Code(err)!=codes.FailedPrecondition||calls!=before{t.Fatal(err)}
}

func TestRerankerSnapshotAndSourceMismatchIntegration(t *testing.T){
    f,mgmt:=newRerankerFixture(t);ctx:=context.Background();server:=&RerankingServer{Service:f.s}
    request:=&rerankingv1.GetProfileRequest{DeploymentId:f.request.DeploymentId}
    if _,err:=server.GetProfile(ctx,request);err!=nil{t.Fatal(err)}
    mgmt.version.RepoId="other/model"
    if _,err:=server.GetProfile(ctx,request);status.Code(err)!=codes.FailedPrecondition{t.Fatal("different source accepted",err)}
    mgmt.version.RepoId=serving.RerankerRepository
    filename:=filepath.Join(f.s.runtime.config.CacheDir,request.DeploymentId,"model","tokenizer.json")
    if err:=os.WriteFile(filename,[]byte(`{}`),0600);err!=nil{t.Fatal(err)}
    if _,err:=server.GetProfile(ctx,request);status.Code(err)!=codes.FailedPrecondition{t.Fatal("tampered tokenizer accepted",err)}
}
