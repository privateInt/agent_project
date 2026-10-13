package runtime
import("os";"path/filepath";"strings";"testing";deploymentv1 "model-platform/gen/deployment/v1")
func rerankingFiles()map[string]string{return map[string]string{
"config.json":`{
  "_name_or_path": "./model/model_data",
  "architectures": [
    "XLMRobertaForSequenceClassification"
  ],
  "attention_probs_dropout_prob": 0.1,
  "bos_token_id": 0,
  "classifier_dropout": null,
  "eos_token_id": 2,
  "hidden_act": "gelu",
  "hidden_dropout_prob": 0.1,
  "hidden_size": 1024,
  "id2label": {
    "0": "LABEL_0"
  },
  "initializer_range": 0.02,
  "intermediate_size": 4096,
  "label2id": {
    "LABEL_0": 0
  },
  "layer_norm_eps": 1e-05,
  "max_position_embeddings": 514,
  "model_type": "xlm-roberta",
  "num_attention_heads": 16,
  "num_hidden_layers": 24,
  "output_past": true,
  "pad_token_id": 1,
  "position_embedding_type": "absolute",
  "torch_dtype": "float32",
  "transformers_version": "4.36.1",
  "type_vocab_size": 1,
  "use_cache": true,
  "vocab_size": 250002
}`,
"tokenizer_config.json":`{
  "added_tokens_decoder": {
    "0": {
      "content": "<s>",
      "lstrip": false,
      "normalized": false,
      "rstrip": false,
      "single_word": false,
      "special": true
    },
    "1": {
      "content": "<pad>",
      "lstrip": false,
      "normalized": false,
      "rstrip": false,
      "single_word": false,
      "special": true
    },
    "2": {
      "content": "</s>",
      "lstrip": false,
      "normalized": false,
      "rstrip": false,
      "single_word": false,
      "special": true
    },
    "3": {
      "content": "<unk>",
      "lstrip": false,
      "normalized": false,
      "rstrip": false,
      "single_word": false,
      "special": true
    },
    "250001": {
      "content": "<mask>",
      "lstrip": true,
      "normalized": true,
      "rstrip": false,
      "single_word": false,
      "special": true
    }
  },
  "additional_special_tokens": [],
  "bos_token": "<s>",
  "clean_up_tokenization_spaces": true,
  "cls_token": "<s>",
  "eos_token": "</s>",
  "mask_token": "<mask>",
  "model_max_length": 512,
  "pad_token": "<pad>",
  "sep_token": "</s>",
  "sp_model_kwargs": {},
  "tokenizer_class": "XLMRobertaTokenizer",
  "unk_token": "<unk>"
}`,
"tokenizer.json":`{"version":"1.0","truncation":{"direction":"Right","max_length":512,"strategy":"LongestFirst","stride":0},"padding":{"strategy":"BatchLongest","direction":"Right","pad_to_multiple_of":null,"pad_id":1,"pad_type_id":0,"pad_token":"<pad>"},"post_processor":{"type":"TemplateProcessing","single":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"pair":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"Sequence":{"id":"B","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"special_tokens":{"</s>":{"id":"</s>","ids":[2],"tokens":["</s>"]},"<s>":{"id":"<s>","ids":[0],"tokens":["<s>"]}}}}`,"model.safetensors":"fixture weights",
}}
func TestRerankerSnapshotPolicyAndTampering(t *testing.T){
    for _,mode:=range []string{"ok","multilabel","wrong-architecture","wrong-pair","wrong-tokenizer","tampered","symlink","missing"}{t.Run(mode,func(t *testing.T){
        files:=rerankingFiles()
        switch mode{
        case "multilabel":files["config.json"]=strings.Replace(files["config.json"],`"0": "LABEL_0"`,`"0": "LABEL_0", "1":"LABEL_1"`,1)
        case "wrong-architecture":files["config.json"]=strings.Replace(files["config.json"],"XLMRobertaForSequenceClassification","XLMRobertaModel",1)
        case "wrong-pair":files["tokenizer.json"]=strings.Replace(files["tokenizer.json"],`"ids":[2]`,`"ids":[3]`,1)
        case "wrong-tokenizer":files["tokenizer_config.json"]=strings.Replace(files["tokenizer_config.json"],"XLMRobertaTokenizer","BertTokenizer",1)
        case "missing":delete(files,"tokenizer.json")
        }
        root,p:=embeddingCache(t,files);path:=filepath.Join(root,p.DeploymentID,"model","tokenizer.json")
        if mode=="tampered"{if e:=os.WriteFile(path,[]byte("tampered"),0600);e!=nil{t.Fatal(e)}}
        if mode=="symlink"{if e:=os.Rename(path,path+".other");e!=nil{t.Fatal(e)};if e:=os.Symlink(path+".other",path);e!=nil{t.Fatal(e)}}
        hash,e:=ValidateRerankerSnapshot(root,p)
        if mode=="ok"{if e!=nil||len(hash)!=64{t.Fatal(hash,e)}}else if e==nil{t.Fatal("invalid snapshot accepted")}
    })}
}
func TestRerankerProfileKeepsExistingPlansAndPinsScoring(t *testing.T){
    spec,e:=Normalize(&deploymentv1.ServingSpec{Engine:"vllm",Profile:RerankerProfile});if e!=nil||spec.MaxModelLen!=512{t.Fatal(spec,e)}
    for _,n:=range []int32{128,511,513,2048}{if _,e=Normalize(&deploymentv1.ServingSpec{Engine:"vllm",Profile:RerankerProfile,MaxModelLen:n});e==nil{t.Fatal("invalid pair limit")}}
    for _,kind:=range []string{"llm","reranker"}{if e=ValidateModelProfile(RerankerProfile,kind,RerankerRepository,"snapshot");e!=nil{t.Fatal(e)}}
    for _,args:=range [][4]string{{"","llm",RerankerRepository,"snapshot"},{RerankerProfile,"embedding",RerankerRepository,"snapshot"},{RerankerProfile,"reranker","wrong/repo","snapshot"},{RerankerProfile,"reranker",RerankerRepository,"adapter"}}{if ValidateModelProfile(args[0],args[1],args[2],args[3])==nil{t.Fatal("unsupported plan")}}
    p:=testPlan();old:=p.Hash();args,e:=CreateArgs(Config{Network:"ops-infra",Volume:"cache"},p)
    if e!=nil||p.Hash()!=old||strings.Contains(strings.Join(args," "),"--pooler-config"){t.Fatal("legacy plan changed",e)}
    p.Spec=spec;args,e=CreateArgs(Config{Network:"ops-infra",Volume:"cache"},p)
    joined:=strings.Join(args," ");if e!=nil||!strings.Contains(joined,`--pooler-config {"use_activation":false}`)||!strings.Contains(joined,"--runner pooling"){t.Fatal(args,e)}
}
