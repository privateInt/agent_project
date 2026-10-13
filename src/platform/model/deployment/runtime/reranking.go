package runtime

import (
    "crypto/sha256"
    "encoding/hex"
    "encoding/json"
    "fmt"
    "os"
    "path/filepath"
    "reflect"

    "github.com/google/uuid"
    "model-platform/internal/reranking"
)

const RerankerProfile = reranking.ProfileID
const RerankerRepository = reranking.Repository

// ValidateRerankerSnapshot reads only authenticated metadata/tokenizer files,
// not multi-GB weights on each inference. Prepare already verifies every weight.
func ValidateRerankerSnapshot(root string, p Plan) (string, error) {
    id, err := uuid.Parse(p.DeploymentID)
    if err != nil || id == uuid.Nil || id.String() != p.DeploymentID || p.Source.Adapter != nil {
        return "", fmt.Errorf("reranker requires a canonical snapshot deployment")
    }
    dir := filepath.Join(root, p.DeploymentID, "model")
    for _, name := range []string{root, filepath.Join(root, p.DeploymentID), dir} {
        info, e := os.Lstat(name)
        if e != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 { return "", fmt.Errorf("unsafe reranker cache directory") }
    }
    manifestPath := filepath.Join(dir, "_manifest.json")
    info, err := os.Lstat(manifestPath)
    if err != nil || !info.Mode().IsRegular() || info.Size() > 16<<20 { return "", fmt.Errorf("reranker manifest unavailable") }
    raw, err := os.ReadFile(manifestPath)
    if err != nil { return "", err }
    manifest, err := parseSnapshot(raw, p.Source)
    if err != nil { return "", err }
    files := map[string]snapshotFile{}
    for _, f := range manifest.Files { files[f.Path] = f }
    read := func(name string, dest any) error {
        f, ok := files[name]
        limit := int64(1<<20)
        if dest == nil || name == "tokenizer.json" { limit = 128<<20 }
        if !ok || f.Size <= 0 || f.Size > limit { return fmt.Errorf("reranker metadata missing/oversized: %s", name) }
        filename := filepath.Join(dir, filepath.FromSlash(name))
        for parent := filepath.Dir(filename); parent != filepath.Dir(dir); parent = filepath.Dir(parent) {
            info, e := os.Lstat(parent)
            if e != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 { return fmt.Errorf("unsafe reranker metadata directory") }
        }
        if e := checkFile(filename, f); e != nil { return e }
        if dest != nil {
            data, e := os.ReadFile(filename)
            if e != nil { return e }
            if e = json.Unmarshal(data, dest); e != nil { return fmt.Errorf("invalid reranker metadata: %s", name) }
        }
        return nil
    }
    var config struct {
        ModelType string `json:"model_type"`
        Architectures []string `json:"architectures"`
        HiddenSize int `json:"hidden_size"`
        MaxPosition int `json:"max_position_embeddings"`
        VocabSize int `json:"vocab_size"`
        Labels map[string]string `json:"id2label"`
        NumLabels *int `json:"num_labels"`
        BOS *int `json:"bos_token_id"`
        EOS *int `json:"eos_token_id"`
        PAD *int `json:"pad_token_id"`
        AutoMap map[string]any `json:"auto_map"`
    }
    if err=read("config.json",&config);err!=nil{return "",err}
    if config.ModelType!="xlm-roberta" || len(config.Architectures)!=1 || config.Architectures[0]!="XLMRobertaForSequenceClassification" || config.HiddenSize!=1024 || config.MaxPosition!=514 || config.VocabSize!=250002 || len(config.Labels)!=1 || config.Labels["0"]=="" || (config.NumLabels!=nil && *config.NumLabels!=1) || config.BOS==nil || *config.BOS!=0 || config.EOS==nil || *config.EOS!=2 || config.PAD==nil || *config.PAD!=1 || len(config.AutoMap)!=0 {
        return "",fmt.Errorf("snapshot must match the ko-reranker-v1 single-logit architecture")
    }
    var tokenizerConfig struct { Class string `json:"tokenizer_class"`; Max int `json:"model_max_length"`; CLS string `json:"cls_token"`; SEP string `json:"sep_token"`; AutoMap map[string]any `json:"auto_map"` }
    if err=read("tokenizer_config.json",&tokenizerConfig);err!=nil{return "",err}
    if (tokenizerConfig.Class!="XLMRobertaTokenizer" && tokenizerConfig.Class!="XLMRobertaTokenizerFast") || tokenizerConfig.Max!=512 || tokenizerConfig.CLS!="<s>" || tokenizerConfig.SEP!="</s>" || len(tokenizerConfig.AutoMap)!=0 {return "",fmt.Errorf("unsupported reranker tokenizer")}
    var tokenizer struct{Truncation any `json:"truncation"`;Post any `json:"post_processor"`}
    if err=read("tokenizer.json",&tokenizer);err!=nil{return "",err}
    var expectedPost, savedTrainingTruncation any
    _=json.Unmarshal([]byte(`{"type":"TemplateProcessing","single":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"pair":[{"SpecialToken":{"id":"<s>","type_id":0}},{"Sequence":{"id":"A","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}},{"Sequence":{"id":"B","type_id":0}},{"SpecialToken":{"id":"</s>","type_id":0}}],"special_tokens":{"</s>":{"id":"</s>","ids":[2],"tokens":["</s>"]},"<s>":{"id":"<s>","ids":[0],"tokens":["<s>"]}}}`),&expectedPost)
    _=json.Unmarshal([]byte(`{"direction":"Right","max_length":512,"strategy":"LongestFirst","stride":0}`),&savedTrainingTruncation)
    // HF stores training-time truncation in this tokenizer. Inference uses
    // untruncated token counts and rejects pair overflow before /score.
    if !reflect.DeepEqual(tokenizer.Post,expectedPost) || (tokenizer.Truncation!=nil && !reflect.DeepEqual(tokenizer.Truncation,savedTrainingTruncation)) {return "",fmt.Errorf("unsupported reranker pair tokenization policy")}
    hashes:=map[string]string{}
    for _,name:=range []string{"tokenizer.json","tokenizer_config.json","special_tokens_map.json","sentencepiece.bpe.model","added_tokens.json"}{
        if _,ok:=files[name];!ok && name!="tokenizer.json" && name!="tokenizer_config.json"{continue}
        if err=read(name,nil);err!=nil{return "",err}
        hashes[name]=files[name].SHA256
    }
    raw,_=json.Marshal(hashes);digest:=sha256.Sum256(raw)
    return hex.EncodeToString(digest[:]),nil
}
