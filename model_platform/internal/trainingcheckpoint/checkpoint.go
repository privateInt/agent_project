// Package trainingcheckpoint reads only platform-sealed local checkpoints.
// Receipts contain private execution identity; REST returns summaries only.
package trainingcheckpoint

import (
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "errors"
 "fmt"
 "os"
 "path/filepath"
 "regexp"
 "sort"
 "strings"

 "github.com/google/uuid"
 trainingv1 "model-platform/gen/training/v1"
)

const MaxReceipt = 4 << 20
var digestPattern=regexp.MustCompile(`^[0-9a-f]{64}$`)
var directoryPattern=regexp.MustCompile(`^checkpoint-([1-9][0-9]*)$`)
type File struct {Path string `json:"path"`;Size int64 `json:"size_bytes"`;SHA256 string `json:"sha256"`}
type Receipt struct {
 Version int `json:"version"`
 RunID string `json:"training_run_id"`
 OperationID string `json:"operation_id"`
 DispatchToken string `json:"dispatch_token"`
 InstanceID string `json:"execution_instance_id"`
 Node string `json:"execution_node"`
 SourceID string `json:"source_model_version_id"`
 Training json.RawMessage `json:"training"`
 Dataset json.RawMessage `json:"dataset"`
 Evaluation json.RawMessage `json:"evaluation,omitempty"`
 Step int32 `json:"step"`
 MaxSteps int32 `json:"max_steps"`
 Signature string `json:"signature"`
 Files []File `json:"files"`
 Size int64 `json:"size_bytes"`
}
func ID(s string)bool{u,e:=uuid.Parse(s);return e==nil&&u!=uuid.Nil&&u.String()==s}
func Digest(b []byte)string{s:=sha256.Sum256(b);return hex.EncodeToString(s[:])}
func RealDirectory(p string)error{i,e:=os.Lstat(p);if e!=nil{return e};if !i.IsDir()||i.Mode()&os.ModeSymlink!=0{return errors.New("checkpoint path is not a real directory")};return nil}
func(r Receipt)Validate()error{
 if r.Version!=1||!ID(r.RunID)||!ID(r.OperationID)||!ID(r.DispatchToken)||!ID(r.InstanceID)||!ID(r.SourceID)||r.Step<=0||r.Step>r.MaxSteps||!digestPattern.MatchString(r.Signature)||len(r.Files)==0||len(r.Files)>4096{return errors.New("invalid checkpoint receipt")}
 seen:=map[string]bool{};var total int64
 for _,f:=range r.Files{
  if f.Path==""||strings.Contains(f.Path,"\\")||filepath.IsAbs(f.Path)||filepath.ToSlash(filepath.Clean(f.Path))!=f.Path||f.Path=="."||strings.HasPrefix(f.Path,"../")||seen[f.Path]||f.Size<=0||f.Size>1<<50||!digestPattern.MatchString(f.SHA256){return errors.New("invalid checkpoint file manifest")}
  seen[f.Path]=true;total+=f.Size
 }
 for _,name:=range []string{"adapter_config.json","adapter_model.safetensors","optimizer.pt","scheduler.pt","rng_state.pth","trainer_state.json","training_args.bin"}{if !seen[name]{return fmt.Errorf("checkpoint missing %s",name)}}
 if total!=r.Size{return errors.New("checkpoint size mismatch")};return nil
}
func Read(savedDir string,step int32)(Receipt,string,error){
 var r Receipt
 if step<=0{return r,"",errors.New("checkpoint step must be positive")}
 dir:=filepath.Join(savedDir,"checkpoints");if e:=RealDirectory(dir);e!=nil{return r,"",e}
 dir=filepath.Join(dir,fmt.Sprintf("checkpoint-%d",step));if e:=RealDirectory(dir);e!=nil{return r,"",e}
 path:=filepath.Join(dir,"receipt.json");info,e:=os.Lstat(path);if e!=nil{return r,"",e};if !info.Mode().IsRegular()||info.Size()>MaxReceipt{return r,"",errors.New("invalid checkpoint receipt file")}
 raw,e:=os.ReadFile(path);if e!=nil{return r,"",e};if len(raw)>MaxReceipt{return r,"",errors.New("checkpoint receipt too large")}
 if e=json.Unmarshal(raw,&r);e!=nil{return r,"",e};if e=r.Validate();e!=nil{return r,"",e};if r.Step!=step{return r,"",errors.New("checkpoint step mismatch")}
 return r,Digest(raw),nil
}
func Steps(savedDir string)([]int32,error){
 dir:=filepath.Join(savedDir,"checkpoints");if e:=RealDirectory(dir);e!=nil{if os.IsNotExist(e){return []int32{},nil};return nil,e}
 items,e:=os.ReadDir(dir);if e!=nil{return nil,e};out:=[]int32{}
 for _,item:=range items{if directoryPattern.MatchString(item.Name()) {var step int32;if _,e=fmt.Sscanf(item.Name(),"checkpoint-%d",&step);e==nil&&step>0{out=append(out,step)}}}
 sort.Slice(out,func(i,j int)bool{return out[i]>out[j]});return out,nil
}

type Resume struct {SourceRunID string `json:"source_training_run_id,omitempty"`;Step int32 `json:"checkpoint_step,omitempty"`;SHA256 string `json:"checkpoint_sha256,omitempty"`}
func(r Resume)Proto()*trainingv1.TrainingResume{if r.SourceRunID==""{return nil};return &trainingv1.TrainingResume{SourceTrainingRunId:r.SourceRunID,CheckpointStep:r.Step,CheckpointSha256:r.SHA256}}
func FromProto(r *trainingv1.TrainingResume)Resume{return Resume{r.GetSourceTrainingRunId(),r.GetCheckpointStep(),r.GetCheckpointSha256()}}

type Evidence struct {
 Version int `json:"version"`;RunID string `json:"training_run_id"`
 SourceRunID string `json:"source_training_run_id"`;SHA256 string `json:"checkpoint_sha256"`
 StartStep int32 `json:"start_step"`;FinalStep int32 `json:"final_step"`;ExecutedSteps int32 `json:"executed_steps"`
 ModelLoaded bool `json:"model_loaded"`;OptimizerLoaded bool `json:"optimizer_loaded"`;SchedulerLoaded bool `json:"scheduler_loaded"`;RNGLoaded bool `json:"rng_loaded"`
}
func ValidateEvidence(e Evidence,id string,total int32,r Resume)error{
 if e.Version!=1||e.RunID!=id||e.SourceRunID!=r.SourceRunID||e.SHA256!=r.SHA256||e.StartStep!=r.Step||e.FinalStep!=total||e.ExecutedSteps!=total-r.Step||e.ExecutedSteps<=0{return errors.New("checkpoint training progress evidence mismatch")}
 if r.SourceRunID!=""&&(!ID(r.SourceRunID)||!digestPattern.MatchString(r.SHA256)||!e.ModelLoaded||!e.OptimizerLoaded||!e.SchedulerLoaded||!e.RNGLoaded){return errors.New("checkpoint training state was not restored")};return nil
}
