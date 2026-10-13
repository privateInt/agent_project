package runtime

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"model-platform/internal/artifacts"
)

func putMetadata(t *testing.T, dir, name, data string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(data), 0o644); err != nil { t.Fatal(err) }
}

func savedEmbeddingFixture(t *testing.T) (string, string, map[string]fixtureTensor, []byte, Plan) {
	t.Helper()
	base, source, values, config := projectionFixture(t)
	putMetadata(t, base, "config.json", `{"model_type":"gemma3_text","architectures":["Gemma3ForCausalLM"],"vocab_size":2,"hidden_size":2,"tie_word_embeddings":true}`)
	// Same vocabulary in both tokenizers, one existing AddedToken marked
	// special by the trainer, and one extra embedding row (the reported case).
	tokenizer := `{"model":{"vocab":{"hello":0,"<mask>":1,"<image>":2}},"added_tokens":[{"id":1,"content":"<mask>","normalized":false,"special":false},{"id":2,"content":"<image>","normalized":false,"special":true}]}`
	putMetadata(t, base, "tokenizer.json", tokenizer)
	putMetadata(t, source, "tokenizer.json", strings.Replace(tokenizer, `"special":false`, `"special":true`, 1))
	putMetadata(t, source, "tokenizer_config.json", `{"chat_template":"{{ messages[0]['content'] }}"}`)
	values["base_model.model.model.embed_tokens.weight"] = fixtureTensor{"BF16", []int64{3,2}, []float32{9,2,3,4,5,6}}
	values["base_model.model.lm_head.base_layer.weight"] = fixtureTensor{"BF16", []int64{3,2}, []float32{7,2,3,4,8,9}}
	putTensors(t, filepath.Join(source, "adapter_model.safetensors"), values)
	p := testPlan()
	p.Source = Source{SHA256: strings.Repeat("a",64), Adapter: &AdapterSource{ServingFormat:2, SHA256: strings.Repeat("b",64)}}
	return base, source, values, config, p
}

func TestSavedEmbeddingBasePreservesWeightsAndTokenizer(t *testing.T) {
	base, source, _, config, p := savedEmbeddingFixture(t)
	ctx := context.Background()
	cfg, err := parseAdapterConfig(config)
	if err != nil { t.Fatal(err) }
	target := filepath.Join(filepath.Dir(base), "adapter-base")
	baseHash, _, _ := artifacts.HashFile(filepath.Join(base, "model.safetensors"))
	sourceHash, _, _ := artifacts.HashFile(filepath.Join(source, "adapter_model.safetensors"))
	// A stale original shard index must not accompany our new single file.
	putMetadata(t, base, "model.safetensors.index.json", `{"weight_map":{"w":"missing-original-shard"}}`)
	if err := prepareAdapterBase(ctx, base, source, target, p, cfg); err != nil { t.Fatal(err) }
	got, err := baseTensors(target)
	if err != nil { t.Fatal(err) }
	all, _ := tensorHeader(filepath.Join(source, "adapter_model.safetensors"))
	for dst, src := range map[string]string{
		"model.embed_tokens.weight":"base_model.model.model.embed_tokens.weight",
		"lm_head.weight":"base_model.model.lm_head.base_layer.weight",
	} {
		equal, err := equalTensor(ctx, got[dst], all[src])
		if err != nil || !equal || !reflect.DeepEqual(got[dst].Shape, []int64{3,2}) { t.Fatal("saved matrix changed or was truncated", dst, err) }
	}
	original, _ := baseTensors(base)
	q := "model.layers.0.self_attn.q_proj.weight"
	if equal, err := equalTensor(ctx, original[q], got[q]); err != nil || !equal { t.Fatal("base linear weight changed", err) }
	if len(got) != 3 { t.Fatal("wrong model tensors", len(got)) }
	meta, err := tokenizerJSON(target, "config.json")
	if err != nil || meta["vocab_size"] != float64(3) || meta["tie_word_embeddings"] != false { t.Fatal("wrong restored model config", meta, err) }
	for _, name := range []string{"tokenizer.json", "tokenizer_config.json"} {
		want, _ := os.ReadFile(filepath.Join(source,name)); got, _ := os.ReadFile(filepath.Join(target,name))
		if string(want) != string(got) { t.Fatal("saved tokenizer not preserved byte-for-byte", name) }
	}
	if _, err := os.Stat(filepath.Join(target,"model.safetensors.index.json")); !os.IsNotExist(err) { t.Fatal("original shard index leaked") }
	receipt, err := tokenizerJSON(target,"serving-base.json")
	if err != nil || receipt["restored_vocab_size"] != float64(3) || receipt["tokenizer_source"] != "adapter" { t.Fatal("missing restoration evidence",receipt,err) }
	projection := filepath.Join(filepath.Dir(base),"adapter")
	if err := projectAdapter(ctx,target,source,projection,p,cfg,config); err != nil { t.Fatal(err) }
	lora, err := tensorHeader(filepath.Join(projection,"adapter_model.safetensors"))
	if err != nil || len(lora) != 2 { t.Fatal("full matrices were sent to LoRA loader",err) }
	for name, tensor := range lora {
		if equal, err := equalTensor(ctx,tensor,all[name]); err != nil || !equal { t.Fatal("LoRA tensor changed",name,err) }
	}
	if err := prepareAdapterBase(ctx,base,source,target,p,cfg); err != nil { t.Fatal("valid cache replay failed",err) }
	putMetadata(t,target,"model.safetensors","corrupt")
	if prepareAdapterBase(ctx,base,source,target,p,cfg) == nil { t.Fatal("corrupt prepared model reused") }
	afterBase,_,_ := artifacts.HashFile(filepath.Join(base,"model.safetensors"))
	afterSource,_,_ := artifacts.HashFile(filepath.Join(source,"adapter_model.safetensors"))
	if baseHash != afterBase || sourceHash != afterSource { t.Fatal("an original artifact was modified") }
}

func TestSavedEmbeddingRejectsIncompatibleInput(t *testing.T) {
	for _, kind := range []string{"missing-head","hidden-size","head-rows","token-id","encoding-flag","token-range","missing-tokenizer-config","other-model","other-tensor","duplicate-embedding"} {
		t.Run(kind,func(t *testing.T) {
			base,source,values,config,p := savedEmbeddingFixture(t)
			switch kind {
			case "missing-head": delete(values,"base_model.model.lm_head.base_layer.weight")
			case "hidden-size": v:=values["base_model.model.model.embed_tokens.weight"];v.shape=[]int64{2,3};values["base_model.model.model.embed_tokens.weight"]=v
			case "head-rows": values["base_model.model.lm_head.base_layer.weight"]=fixtureTensor{"BF16",[]int64{2,2},[]float32{1,2,3,4}}
			case "token-id", "encoding-flag", "token-range":
				data,_:=os.ReadFile(filepath.Join(source,"tokenizer.json"));raw:=string(data)
				if kind=="token-id" {raw=strings.Replace(raw,`"id":1`,`"id":0`,1)}
				if kind=="encoding-flag" {raw=strings.Replace(raw,`"normalized":false`,`"normalized":true`,1)}
				if kind=="token-range" {raw=strings.Replace(raw,`"<image>":2`,`"<image>":3`,1);putMetadata(t,base,"tokenizer.json",raw)}
				putMetadata(t,source,"tokenizer.json",raw)
			case "missing-tokenizer-config": if err:=os.Remove(filepath.Join(source,"tokenizer_config.json"));err!=nil{t.Fatal(err)}
			case "other-model": putMetadata(t,base,"config.json",`{"model_type":"other","tie_word_embeddings":true}`)
			case "other-tensor": values["base_model.model.model.norm.weight"]=fixtureTensor{"BF16",[]int64{2},[]float32{1,1}}
			case "duplicate-embedding": values["base_model.model.lm_head.weight"]=values["base_model.model.lm_head.base_layer.weight"]
			}
			putTensors(t,filepath.Join(source,"adapter_model.safetensors"),values)
			cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
			target:=filepath.Join(filepath.Dir(base),"adapter-base")
			if prepareAdapterBase(context.Background(),base,source,target,p,cfg)==nil {t.Fatal("incompatible input accepted")}
			if _,err:=os.Lstat(target);!os.IsNotExist(err){t.Fatal("partial model published")}
		})
	}
}

func TestAdapterServingFormatRuntimePaths(t *testing.T) {
	p:=testPlan();p.Source.Adapter=&AdapterSource{ServingFormat:1}
	first:=p.Hash()
	for _,format:=range []int{1,2} {
		p.Source.Adapter.ServingFormat=format
		args,err:=CreateArgs(Config{Network:"test",Volume:"cache"},p);if err!=nil{t.Fatal(err)}
		joined:=strings.Join(args," ");dir:="model";if format==2{dir="adapter-base"}
		if !strings.Contains(joined,"--model /models/"+p.DeploymentID+"/"+dir+" ") || !strings.Contains(joined,"--enable-lora") {t.Fatal("wrong serving model path",joined)}
	}
	if p.Hash()==first {t.Fatal("format change did not change container ownership hash")}
	p.Source.Adapter.ServingFormat=1
	if p.Hash()!=first {t.Fatal("legacy format hash changed")}
	p.Source.Adapter.ServingFormat=3
	if _,err:=CreateArgs(Config{},p);err==nil{t.Fatal("unknown future format launched")}
}

func TestAdapterFormatTwoManifestAndCache(t *testing.T) {
	base,source,_,_,p:=savedEmbeddingFixture(t)
	putMetadata(t,source,"special_tokens_map.json",`{"mask_token":"<mask>"}`)
	putMetadata(t,source,"optimizer.pt","unused optimizer state")
	store:=&servingMemoryStore{objects:map[string][]byte{}}
	var manifest snapshotManifest
	entries,err:=os.ReadDir(base);if err!=nil{t.Fatal(err)}
	for _,entry:=range entries {
		data,err:=os.ReadFile(filepath.Join(base,entry.Name()));if err!=nil{t.Fatal(err)}
		store.objects["base/"+entry.Name()]=data
		manifest.Files=append(manifest.Files,snapshotFile{Path:entry.Name(),SHA256:artifacts.SHA256(data),Size:int64(len(data))})
		p.Source.SizeBytes+=int64(len(data))
	}
	encoded,err:=json.Marshal(manifest);if err!=nil{t.Fatal(err)}
	p.Source.URI="s3://model-artifacts/base/";p.Source.SHA256=artifacts.SHA256(encoded)
	store.objects["base/_manifest.json"]=encoded
	m,encoded,digest,err:=artifacts.Build(source);if err!=nil{t.Fatal(err)}
	p.Source.Adapter=&AdapterSource{ServingFormat:2,URI:"s3://model-artifacts/adapter/",SHA256:digest,SizeBytes:m.TotalBytes,FileCount:int64(m.FileCount)}
	store.objects["adapter/"+artifacts.ManifestName]=encoded
	for _,entry:=range m.Files {data,err:=os.ReadFile(filepath.Join(source,entry.Path));if err!=nil{t.Fatal(err)};store.objects["adapter/"+entry.Path]=data}
	fetcher:=SnapshotFetcher{Store:store,Root:t.TempDir()}
	if err:=fetcher.Prepare(context.Background(),p);err!=nil{t.Fatal(err)}
	for _,key:=range store.downloads{if strings.Contains(key,"optimizer"){t.Fatal("unused optimizer downloaded")}}
	prepared:=filepath.Join(fetcher.Root,p.DeploymentID,"adapter-base")
	if _,err:=os.Stat(filepath.Join(prepared,"special_tokens_map.json"));err!=nil{t.Fatal("saved tokenizer sidecar lost",err)}
	n:=len(store.downloads)
	if err:=fetcher.Prepare(context.Background(),p);err!=nil{t.Fatal(err)}
	if len(store.downloads)!=n{t.Fatal("verified sources downloaded again")}
	putMetadata(t,filepath.Join(fetcher.Root,p.DeploymentID,"adapter-source"),"special_tokens_map.json",`{}`)
	if fetcher.Prepare(context.Background(),p)==nil{t.Fatal("corrupted source sidecar accepted")}
}

func TestSavedEmbeddingCancellationDoesNotPublish(t *testing.T) {
	base,source,_,config,p:=savedEmbeddingFixture(t)
	cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
	ctx,cancel:=context.WithCancel(context.Background());cancel()
	target:=filepath.Join(filepath.Dir(base),"adapter-base")
	if prepareAdapterBase(ctx,base,source,target,p,cfg)==nil{t.Fatal("cancelled preparation succeeded")}
	if _,err:=os.Lstat(target);!os.IsNotExist(err){t.Fatal("cancelled model published")}
}

func TestFormatTwoKeepsUnchangedAdapterSupport(t *testing.T) {
	base,source,_,config:=projectionFixture(t)
	p:=testPlan();p.Source.Adapter=&AdapterSource{ServingFormat:2}
	cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
	target:=filepath.Join(filepath.Dir(base),"adapter-base")
	if err:=prepareAdapterBase(context.Background(),base,source,target,p,cfg);err!=nil{t.Fatal(err)}
	if err:=projectAdapter(context.Background(),target,source,filepath.Join(filepath.Dir(base),"adapter"),p,cfg,config);err!=nil{t.Fatal(err)}
	metadata,err:=tokenizerJSON(target,"config.json")
	if err!=nil || metadata["tie_word_embeddings"]!=true {t.Fatal("unchanged generic base semantics changed",metadata,err)}
	legacy:=p;legacy.Source.Adapter=&AdapterSource{ServingFormat:1}
	if legacy.ModelDirectory()!="model" {t.Fatal("existing format redirected")}
}

func TestSavedTokenizerInsertionOptionsArePreserved(t *testing.T) {
	for _, tc := range []struct { name, base, saved string }{
		{"explicit-bos", `{}`, `{"add_bos_token":true}`},
		{"disabled-bos", `{"add_bos_token":true}`, `{"add_bos_token":false}`},
		{"enabled-eos", `{"add_eos_token":false}`, `{"add_eos_token":true}`},
		{"omitted-options", `{"add_bos_token":true,"add_eos_token":false}`, `{}`},
	} {
		t.Run(tc.name,func(t *testing.T) {
			base,source,_,config,p:=savedEmbeddingFixture(t)
			putMetadata(t,base,"tokenizer_config.json",tc.base)
			putMetadata(t,source,"tokenizer_config.json",tc.saved)
			putMetadata(t,source,"chat_template.jinja","{{ messages[0]['content'] }}")
			cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
			target:=filepath.Join(filepath.Dir(base),"adapter-base")
			if err:=prepareAdapterBase(context.Background(),base,source,target,p,cfg);err!=nil{t.Fatal(err)}
			actual,err:=os.ReadFile(filepath.Join(target,"tokenizer_config.json"))
			if err!=nil || string(actual)!=tc.saved {t.Fatal("saved insertion options were normalized or overwritten",string(actual),err)}
			original,err:=os.ReadFile(filepath.Join(base,"tokenizer_config.json"))
			if err!=nil || string(original)!=tc.base {t.Fatal("original config modified",err)}
			if err:=projectAdapter(context.Background(),target,source,filepath.Join(filepath.Dir(base),"adapter"),p,cfg,config);err!=nil{t.Fatal(err)}
			// Isolate the insertion settings: the other tokenizer records match.
			savedJSON,err:=os.ReadFile(filepath.Join(source,"tokenizer.json"));if err!=nil{t.Fatal(err)}
			putMetadata(t,base,"tokenizer.json",string(savedJSON))
			// Format 1 still serves with the original base tokenizer.
			if sameTokenizer(base,source)==nil {t.Fatal("legacy base-tokenizer restriction was bypassed")}
		})
	}
}

func TestSavedTokenizerRejectsInvalidOptionsAndChangedTokenIdentity(t *testing.T) {
	for _, tc := range []struct { name, saved string }{
		{"string-bos", `{"add_bos_token":"true"}`},
		{"null-bos", `{"add_bos_token":null}`},
		{"numeric-eos", `{"add_eos_token":1}`},
		{"changed-bos-token", `{"bos_token":"<different>"}`},
		{"changed-eos-token", `{"eos_token":"<different>"}`},
	} {
		t.Run(tc.name,func(t *testing.T) {
			base,source,_,config,p:=savedEmbeddingFixture(t)
			putMetadata(t,source,"tokenizer_config.json",tc.saved)
			cfg,err:=parseAdapterConfig(config);if err!=nil{t.Fatal(err)}
			target:=filepath.Join(filepath.Dir(base),"adapter-base")
			if prepareAdapterBase(context.Background(),base,source,target,p,cfg)==nil {t.Fatal("invalid options or changed token identity accepted")}
			if _,err:=os.Lstat(target);!os.IsNotExist(err){t.Fatal("invalid tokenizer published")}
		})
	}
}
