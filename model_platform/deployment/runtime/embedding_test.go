package runtime

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "os"
    "path/filepath"
    "sort"
    "strings"
    "testing"
    deploymentv1 "model-platform/gen/deployment/v1"
)

func embeddingDigest(b []byte)string{sum:=sha256.Sum256(b);return hex.EncodeToString(sum[:])}
func embeddingFiles()map[string]string{return map[string]string{
    "config.json":`{"model_type":"xlm-roberta","architectures":["XLMRobertaModel"],"hidden_size":1024,"max_position_embeddings":8194,"vocab_size":250002}`,
    "modules.json":`[{"idx":0,"path":"","type":"sentence_transformers.models.Transformer"},{"idx":1,"path":"1_Pooling","type":"sentence_transformers.models.Pooling"},{"idx":2,"path":"2_Normalize","type":"sentence_transformers.models.Normalize"}]`,
    "1_Pooling/config.json":`{"word_embedding_dimension":1024,"pooling_mode_cls_token":true,"pooling_mode_mean_tokens":false,"pooling_mode_max_tokens":false,"pooling_mode_mean_sqrt_len_tokens":false,"pooling_mode_weightedmean_tokens":false,"pooling_mode_lasttoken":false,"include_prompt":true}`,
    "config_sentence_transformers.json":`{"prompts":{"query":"query: "},"default_prompt_name":null,"similarity_fn_name":"cosine"}`,
    "tokenizer.json":`{"fixture":"tokenizer"}`,"tokenizer_config.json":`{"fixture":"tokenizer-config"}`,"model.safetensors":"fixture weights",
}}
func embeddingCache(t *testing.T,files map[string]string)(string,Plan){
    t.Helper();root:=t.TempDir();p:=testPlan();p.DeploymentID="44247297-5184-4a36-9225-7791c80d3add"
    dir:=filepath.Join(root,p.DeploymentID,"model")
    names:=[]string{};for name:=range files{names=append(names,name)};sort.Strings(names)
    manifest:=snapshotManifest{};p.Source.SizeBytes=0
    for _,name:=range names{
        data:=[]byte(files[name]);target:=filepath.Join(dir,name)
        if err:=os.MkdirAll(filepath.Dir(target),0700);err!=nil{t.Fatal(err)}
        if err:=os.WriteFile(target,data,0600);err!=nil{t.Fatal(err)}
        manifest.Files=append(manifest.Files,snapshotFile{Path:name,SHA256:embeddingDigest(data),Size:int64(len(data))});p.Source.SizeBytes+=int64(len(data))
    }
    raw,_:=json.Marshal(manifest);p.Source.SHA256=embeddingDigest(raw)
    if err:=os.WriteFile(filepath.Join(dir,"_manifest.json"),raw,0600);err!=nil{t.Fatal(err)}
    return root,p
}

func TestEmbeddingSnapshotAuthenticatesMetadataAndTokenizer(t *testing.T){
    root,p:=embeddingCache(t,embeddingFiles());tokenizer,err:=ValidateEmbeddingSnapshot(root,p)
    if err!=nil || len(tokenizer)!=64{t.Fatal(tokenizer,err)}
    if err=os.WriteFile(filepath.Join(root,p.DeploymentID,"model","tokenizer.json"),[]byte(`{"fixture":"tampering"}`),0600);err!=nil{t.Fatal(err)}
    if _,err=ValidateEmbeddingSnapshot(root,p);err==nil{t.Fatal("tampered tokenizer accepted")}
}

func TestEmbeddingSnapshotRejectsDifferentSemanticsAndUnsafeCache(t *testing.T){
    for _,kind:=range []string{"pooling","prefix","modules","architecture","missing","symlink","manifest","traversal"}{t.Run(kind,func(t *testing.T){
        files:=embeddingFiles()
        switch kind{
        case "pooling":files["1_Pooling/config.json"]=`{"word_embedding_dimension":1024,"pooling_mode_mean_tokens":true,"include_prompt":true}`
        case "prefix":files["config_sentence_transformers.json"]=`{"prompts":{"query":"wrong"},"similarity_fn_name":"cosine"}`
        case "modules":files["modules.json"]=`[]`
        case "architecture":files["config.json"]=`{"model_type":"gemma3"}`
        case "missing":delete(files,"tokenizer_config.json")
        }
        root,p:=embeddingCache(t,files)
        switch kind{
        case "symlink":dir:=filepath.Join(root,p.DeploymentID,"model","1_Pooling");if err:=os.Rename(dir,dir+"-elsewhere");err!=nil{t.Fatal(err)};if err:=os.Symlink(dir+"-elsewhere",dir);err!=nil{t.Fatal(err)}
        case "manifest":p.Source.SHA256=strings.Repeat("0",64)
        case "traversal":p.DeploymentID="../other"
        }
        if _,err:=ValidateEmbeddingSnapshot(root,p);err==nil{t.Fatal("unsafe metadata accepted")}
    })}
}

func TestEmbeddingProfileAndRunnerDoNotChangeLegacyPlan(t *testing.T){
    spec,err:=Normalize(&deploymentv1.ServingSpec{Engine:"vllm",Profile:EmbeddingProfile})
    if err!=nil || spec.MaxModelLen!=2048{t.Fatal(spec,err)}
    if _,err=Normalize(&deploymentv1.ServingSpec{Engine:"vllm",Profile:EmbeddingProfile,MaxModelLen:8193});err==nil{t.Fatal("oversized embedding limit")}
    for _,kind:=range []string{"embedding","llm"}{if err=ValidateModelProfile(EmbeddingProfile,kind,EmbeddingRepository,"snapshot");err!=nil{t.Fatal(err)}}
    for _,args:=range [][4]string{{EmbeddingProfile,"reranker",EmbeddingRepository,"snapshot"},{EmbeddingProfile,"embedding","other/repo","snapshot"},{EmbeddingProfile,"embedding",EmbeddingRepository,"adapter"}}{if ValidateModelProfile(args[0],args[1],args[2],args[3])==nil{t.Fatal("invalid model accepted")}}
    p:=testPlan();before:=p.Hash();args,err:=CreateArgs(Config{Network:"ops-infra",Volume:"cache"},p)
    if err!=nil || strings.Contains(strings.Join(args," "),"--runner") || p.Hash()!=before{t.Fatal("legacy plan changed",err)}
    p.Spec.Profile=EmbeddingProfile;args,err=CreateArgs(Config{Network:"ops-infra",Volume:"cache"},p)
    if err!=nil || !strings.Contains(strings.Join(args," "),"--runner pooling") || p.Hash()==before{t.Fatal("new profile omitted",err)}
}
