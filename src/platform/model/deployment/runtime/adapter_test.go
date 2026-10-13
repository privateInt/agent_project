package runtime

import (
	"context"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"model-platform/internal/artifacts"
	"model-platform/internal/execution"
)

type fixtureTensor struct { dtype string; shape []int64; values []float32 }
func putTensors(t *testing.T,name string,values map[string]fixtureTensor) {
	t.Helper();header:=map[string]any{};keys:=[]string{};for k:=range values{keys=append(keys,k)};sort.Strings(keys)
	data:=[]byte{}
	for _,key:=range keys {
		v:=values[key];start:=len(data)
		for _,value:=range v.values {
			bits:=math.Float32bits(value)
			if v.dtype=="BF16" {var b [2]byte;binary.LittleEndian.PutUint16(b[:],uint16(bits>>16));data=append(data,b[:]...)} else {var b [4]byte;binary.LittleEndian.PutUint32(b[:],bits);data=append(data,b[:]...)}
		}
		header[key]=tensorInfo{Dtype:v.dtype,Shape:v.shape,Offsets:[]int64{int64(start),int64(len(data))}}
	}
	raw,err:=json.Marshal(header);if err!=nil{t.Fatal(err)}
	var size [8]byte;binary.LittleEndian.PutUint64(size[:],uint64(len(raw)))
	file:=append(size[:],raw...);file=append(file,data...)
	if err:=os.WriteFile(name,file,0o644);err!=nil{t.Fatal(err)}
}
func projectionFixture(t *testing.T)(string,string,map[string]fixtureTensor,[]byte) {
	t.Helper();dir:=t.TempDir();base,source:=filepath.Join(dir,"model"),filepath.Join(dir,"source")
	for _,name:=range []string{base,source}{if err:=os.Mkdir(name,0o755);err!=nil{t.Fatal(err)}}
	os.WriteFile(filepath.Join(base,"config.json"),[]byte(`{"tie_word_embeddings":true}`),0o644)
	os.WriteFile(filepath.Join(base,"tokenizer_config.json"),[]byte(`{"chat_template":"{{ messages[0]['content'] }}"}`),0o644)
	putTensors(t,filepath.Join(base,"model.safetensors"),map[string]fixtureTensor{
		"model.embed_tokens.weight":{"BF16",[]int64{2,2},[]float32{1,2,3,4}},
		"model.layers.0.self_attn.q_proj.weight":{"BF16",[]int64{2,2},[]float32{1,0,0,1}},
	})
	values:=map[string]fixtureTensor{
		"base_model.model.model.layers.0.self_attn.q_proj.lora_A.weight":{"F32",[]int64{1,2},[]float32{0.1,0.2}},
		"base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight":{"F32",[]int64{2,1},[]float32{0.3,0.4}},
		"base_model.model.model.embed_tokens.weight":{"F32",[]int64{2,2},[]float32{1,2,3,4}},
		"base_model.model.lm_head.base_layer.weight":{"F32",[]int64{2,2},[]float32{1,2,3,4}},
	}
	putTensors(t,filepath.Join(source,"adapter_model.safetensors"),values)
	config:=[]byte(`{"peft_type":"LORA","task_type":"CAUSAL_LM","r":1,"lora_alpha":2,"bias":"none","target_modules":["q_proj"]}`)
	os.WriteFile(filepath.Join(source,"adapter_config.json"),config,0o644)
	return base,source,values,config
}
func TestAdapterProjectionPreservesLoRAAndChecksSavedEmbeddings(t *testing.T) {
	base,source,_,config:=projectionFixture(t);cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
	p:=testPlan();p.Source=Source{SHA256:strings.Repeat("a",64),Adapter:&AdapterSource{ServingFormat:1,SHA256:strings.Repeat("b",64)}}
	target:=filepath.Join(filepath.Dir(base),"adapter")
	original,_,_:=artifacts.HashFile(filepath.Join(source,"adapter_model.safetensors"))
	if err:=projectAdapter(context.Background(),base,source,target,p,cfg,config);err!=nil{t.Fatal(err)}
	projected,err:=tensorHeader(filepath.Join(target,"adapter_model.safetensors"));if err!=nil{t.Fatal(err)}
	if len(projected)!=2{t.Fatal("redundant embeddings remain in serving projection")}
	all,_:=tensorHeader(filepath.Join(source,"adapter_model.safetensors"))
	for name,tensor:=range projected {equal,err:=equalTensor(context.Background(),tensor,all[name]);if err!=nil || !equal{t.Fatal("LoRA values changed",name,err)}}
	after,_,_:=artifacts.HashFile(filepath.Join(source,"adapter_model.safetensors"));if after!=original{t.Fatal("source artifact was changed")}
	if err:=projectAdapter(context.Background(),base,source,target,p,cfg,config);err!=nil{t.Fatal("valid projection replay rejected",err)}
	os.WriteFile(filepath.Join(target,"adapter_model.safetensors"),[]byte("corrupt"),0o644)
	if projectAdapter(context.Background(),base,source,target,p,cfg,config)==nil{t.Fatal("changed projection silently reused")}
}
func TestAdapterRejectsChangedEmbeddingsAndBrokenPairs(t *testing.T) {
	for _,kind:=range []string{"embedding-value","embedding-shape","missing-pair","rank","other-tensor"} {
		t.Run(kind,func(t *testing.T){
			base,source,values,config:=projectionFixture(t)
			key:="base_model.model.model.embed_tokens.weight"
			switch kind {
			case "embedding-value":v:=values[key];v.values=[]float32{9,2,3,4};values[key]=v
			case "embedding-shape":v:=values[key];v.shape=[]int64{1,4};values[key]=v
			case "missing-pair":delete(values,"base_model.model.model.layers.0.self_attn.q_proj.lora_B.weight")
			case "rank":config=[]byte(`{"peft_type":"LORA","task_type":"CAUSAL_LM","r":2,"lora_alpha":4}`)
			case "other-tensor":values["base_model.model.model.norm.weight"]=fixtureTensor{"F32",[]int64{2},[]float32{1,1}}
			}
			putTensors(t,filepath.Join(source,"adapter_model.safetensors"),values)
			all,err:=tensorHeader(filepath.Join(source,"adapter_model.safetensors"));if err!=nil{t.Fatal(err)}
			cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
			if _,_,err:=selectedLoRA(context.Background(),base,all,cfg);err==nil{t.Fatal("unsupported tensor semantics accepted")}
		})
	}
}
func TestAdapterConfigurationRejectsUnsupportedFeatures(t *testing.T) {
	for _,extra:=range []string{`"r":128`,`"modules_to_save":["lm_head"]`,`"use_dora":true`,`"bias":"all"`,`"rank_pattern":{"q_proj":8}`,`"alpha_pattern":{"q_proj":16}`,`"lora_bias":true`,`"target_parameters":["experts.weight"]`} {
		c:=map[string]any{"peft_type":"LORA","task_type":"CAUSAL_LM","r":8,"lora_alpha":16}
		var patch map[string]any;json.Unmarshal([]byte("{"+extra+"}"),&patch);for k,v:=range patch{c[k]=v}
		data,_:=json.Marshal(c);if _,err:=parseAdapterConfig(data);err==nil{t.Fatal(extra)}
	}
}
func TestLoRAModelListAndDockerArguments(t *testing.T) {
	p:=testPlan();p.Source.Adapter=&AdapterSource{ServingFormat:1,ModelVersionID:"adapter"}
	args,err:=CreateArgs(Config{Network:"ops-infra",Volume:"cache"},p);if err!=nil{t.Fatal(err)}
	joined:=strings.Join(args," ")
	if !strings.Contains(joined,"--served-model-name "+p.BaseModelName()) || !strings.Contains(joined,"--enable-lora --max-loras 1 --max-lora-rank 64") {t.Fatal(args)}
	var module map[string]string
	if err:=json.Unmarshal([]byte(args[len(args)-1]),&module);err!=nil{t.Fatal(err)}
	if module["name"]!=p.ModelName() || module["base_model_name"]!=p.BaseModelName() || module["path"]!="/models/"+p.DeploymentID+"/adapter"{t.Fatal(module)}
	models:=map[string]any{"data":[]map[string]string{{"id":p.BaseModelName()},{"id":p.ModelName(),"parent":p.BaseModelName(),"root":module["path"]}}}
	data,_:=json.Marshal(models);if !ValidateModels(data,p){t.Fatal("valid adapter registration rejected")}
	for _,wrong:=range []string{`{"data":[{"id":"`+p.BaseModelName()+`"}]}`,`{"data":[{"id":"`+p.ModelName()+`"}]}`} {if ValidateModels([]byte(wrong),p){t.Fatal("base-only/wrong-parent readiness accepted")}}
}
func TestSnapshotPlanHashRemainsCompatible(t *testing.T) {
	p:=testPlan();p.Source=Source{ModelVersionID:"base",URI:"s3://bucket/base/",SHA256:strings.Repeat("a",64),SizeBytes:100}
	type oldSource struct{ModelVersionID string `json:"model_version_id"`;URI string `json:"uri"`;SHA256 string `json:"sha256"`;SizeBytes int64 `json:"size_bytes"`}
	legacy:=struct{DeploymentID string `json:"deployment_id"`;OperationID string `json:"operation_id"`;ImageID string `json:"image_id"`;Spec Spec `json:"spec"`;Resources execution.Resources `json:"resources"`;Source oldSource `json:"source"`}{p.DeploymentID,p.OperationID,p.ImageID,p.Spec,p.Resources,oldSource{p.Source.ModelVersionID,p.Source.URI,p.Source.SHA256,p.Source.SizeBytes}}
	data,_:=json.Marshal(legacy);if artifacts.SHA256(data)!=p.Hash(){t.Fatal("existing snapshot container ownership hash changed")}
}

type servingMemoryStore struct { objects map[string][]byte; downloads []string }
func(s *servingMemoryStore)Bucket()string{return "model-artifacts"}
func(s *servingMemoryStore)DownloadObject(ctx context.Context,key,dest string)(int64,error){
	if err:=ctx.Err();err!=nil{return 0,err};s.downloads=append(s.downloads,key)
	data,ok:=s.objects[key];if !ok{return 0,fmt.Errorf("missing object: %s",key)}
	if err:=os.MkdirAll(filepath.Dir(dest),0o755);err!=nil{return 0,err};return int64(len(data)),os.WriteFile(dest,data,0o644)
}
func TestAdapterFetcherVerifiesUsedFilesAndSkipsCheckpoints(t *testing.T) {
	base,source,_,_:=projectionFixture(t);p:=testPlan();store:=&servingMemoryStore{objects:map[string][]byte{}}
	baseManifest:=snapshotManifest{}
	for _,name:=range []string{"config.json","model.safetensors","tokenizer_config.json"} {
		data,err:=os.ReadFile(filepath.Join(base,name));if err!=nil{t.Fatal(err)};store.objects["base/"+name]=data
		baseManifest.Files=append(baseManifest.Files,snapshotFile{Path:name,SHA256:artifacts.SHA256(data),Size:int64(len(data))});p.Source.SizeBytes+=int64(len(data))
	}
	manifest,_:=json.Marshal(baseManifest);p.Source.URI="s3://model-artifacts/base/";p.Source.SHA256=artifacts.SHA256(manifest);store.objects["base/_manifest.json"]=manifest
	os.Mkdir(filepath.Join(source,"checkpoint-1"),0o755);os.WriteFile(filepath.Join(source,"checkpoint-1","optimizer.pt"),[]byte("unused"),0o644)
	m,encoded,digest,err:=artifacts.Build(source);if err!=nil{t.Fatal(err)}
	p.Source.Adapter=&AdapterSource{ServingFormat:1,URI:"s3://model-artifacts/adapter/",SHA256:digest,SizeBytes:m.TotalBytes,FileCount:int64(m.FileCount)}
	store.objects["adapter/"+artifacts.ManifestName]=encoded
	for _,entry:=range m.Files {data,_:=os.ReadFile(filepath.Join(source,entry.Path));store.objects["adapter/"+entry.Path]=data}
	fetcher:=SnapshotFetcher{Store:store,Root:t.TempDir()}
	if err:=fetcher.Prepare(context.Background(),p);err!=nil{t.Fatal(err)}
	for _,key:=range store.downloads {if strings.Contains(key,"checkpoint"){t.Fatal("unused checkpoint downloaded")}}
	n:=len(store.downloads)
	if err:=fetcher.Prepare(context.Background(),p);err!=nil{t.Fatal(err)}
	if len(store.downloads)!=n{t.Fatal("verified cached sources redownloaded")}
	p.Source.Adapter.SHA256=strings.Repeat("0",64)
	if fetcher.Prepare(context.Background(),p)==nil{t.Fatal("different manifest reused existing cache")}
}
func TestAdapterTokenizerChangesAreRejected(t *testing.T) {
	base,source,_,_:=projectionFixture(t)
	for _,dir:=range []string{base,source}{os.WriteFile(filepath.Join(dir,"tokenizer.json"),[]byte(`{"model":{"vocab":{"hello":0}}}`),0o644)}
	if err:=sameTokenizer(base,source);err!=nil{t.Fatal(err)}
	os.WriteFile(filepath.Join(source,"tokenizer.json"),[]byte(`{"model":{"vocab":{"hello":1}}}`),0o644)
	if sameTokenizer(base,source)==nil{t.Fatal("different token vocabulary accepted")}
}

func TestSavedChatTemplateAndSpecialTokens(t *testing.T) {
	base, source, _, config := projectionFixture(t)
	chat := []byte("<user>{{ messages[0]['content'] }}</user><assistant>")
	if err := os.WriteFile(filepath.Join(source, "chat_template.jinja"), chat, 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, err := parseAdapterConfig(config)
	if err != nil {
		t.Fatal(err)
	}
	p := testPlan()
	p.Source.Adapter = &AdapterSource{ServingFormat: 1}
	target := filepath.Join(filepath.Dir(base), "adapter")
	if err := projectAdapter(context.Background(), base, source, target, p, cfg, config); err != nil {
		t.Fatal(err)
	}
	got, err := os.ReadFile(filepath.Join(target, "chat_template.jinja"))
	if err != nil || string(got) != string(chat) {
		t.Fatal("trainer chat template was not preserved", err)
	}
	if err := os.WriteFile(filepath.Join(source, "tokenizer_config.json"), []byte(`{"bos_token":"new-bos"}`), 0o644); err != nil {
		t.Fatal(err)
	}
	if sameTokenizer(base, source) == nil {
		t.Fatal("changed special token accepted")
	}
	if err := os.WriteFile(filepath.Join(source, "chat_template.jinja"), nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if _, _, err := servingChatTemplate(base, source); err == nil {
		t.Fatal("empty saved template silently fell back to base")
	}
}

func TestSafetensorsRejectsAmbiguousAndInvalidRanges(t *testing.T) {
	for _, header := range []string{
		`{"w":{"dtype":"F32","shape":[1],"data_offsets":[0,4]},"w":{"dtype":"F32","shape":[1],"data_offsets":[0,4]}}`,
		`{"w":{"dtype":"F32","shape":[2],"data_offsets":[0,4]}}`,
		`{"w":{"dtype":"F32","shape":[1],"data_offsets":[1,5]}}`,
	} {
		var size [8]byte
		binary.LittleEndian.PutUint64(size[:], uint64(len(header)))
		data := append(size[:], []byte(header)...)
		data = append(data, 0, 0, 0, 0)
		name := filepath.Join(t.TempDir(), "weights.safetensors")
		if err := os.WriteFile(name, data, 0o644); err != nil {
			t.Fatal(err)
		}
		if _, err := tensorHeader(name); err == nil {
			t.Fatal("invalid tensor layout accepted", header)
		}
	}
}

func TestFP16EmbeddingComparisonValues(t *testing.T) {
	for _, tc := range []struct {
		bits uint16
		want float32
	}{
		{0x0001, 0x1p-24},
		{0x0400, 0x1p-14},
		{0x3c00, 1},
		{0xc000, -2},
		{0x7bff, 65504},
		{0x7c00, float32(math.Inf(1))},
	} {
		var data [2]byte
		binary.LittleEndian.PutUint16(data[:], tc.bits)
		if got := scalar(data[:], "F16"); got != tc.want {
			t.Fatalf("FP16 %#x: got %g, want %g", tc.bits, got, tc.want)
		}
	}
	if !math.IsNaN(float64(scalar([]byte{0, 0x7e}, "F16"))) {
		t.Fatal("NaN treated as an equal embedding value")
	}
}
