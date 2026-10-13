package trainingcheckpoint

import (
 "encoding/json"
 "os"
 "path/filepath"
 "strings"
 "testing"
 "github.com/google/uuid"
)
func TestCheckpointReceiptAndPathValidation(t *testing.T){
 r:=Receipt{Version:1,RunID:uuid.NewString(),OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),InstanceID:uuid.NewString(),SourceID:uuid.NewString(),Step:2,MaxSteps:4,Signature:strings.Repeat("a",64)}
 for _,name:=range []string{"adapter_config.json","adapter_model.safetensors","optimizer.pt","scheduler.pt","rng_state.pth","trainer_state.json","training_args.bin"}{r.Files=append(r.Files,File{Path:name,Size:1,SHA256:strings.Repeat("b",64)});r.Size++}
 root:=t.TempDir();dir:=filepath.Join(root,"checkpoints","checkpoint-2");if e:=os.MkdirAll(dir,0700);e!=nil{t.Fatal(e)}
 raw,_:=json.Marshal(r);if e:=os.WriteFile(filepath.Join(dir,"receipt.json"),raw,0600);e!=nil{t.Fatal(e)}
 got,d,e:=Read(root,2);if e!=nil||got.Step!=2||d!=Digest(raw){t.Fatal(got,d,e)}
 for _,path:=range []string{"../optimizer.pt","/optimizer.pt","nested/../../optimizer.pt","a\\b"}{bad:=r;bad.Files=append([]File(nil),r.Files...);bad.Files[0].Path=path;if bad.Validate()==nil{t.Fatal("unsafe manifest accepted",path)}}
 if e=os.Rename(dir,filepath.Join(root,"outside"));e!=nil{t.Fatal(e)};if e=os.Symlink(filepath.Join(root,"outside"),dir);e!=nil{t.Fatal(e)}
 if _,_,e=Read(root,2);e==nil{t.Fatal("symlink checkpoint accepted")}
}
func TestResumeEvidenceRequiresAllStateAndRemainingSteps(t *testing.T){
 r:=Resume{SourceRunID:uuid.NewString(),Step:2,SHA256:strings.Repeat("a",64)}
 e:=Evidence{Version:1,RunID:uuid.NewString(),SourceRunID:r.SourceRunID,SHA256:r.SHA256,StartStep:2,FinalStep:4,ExecutedSteps:2,ModelLoaded:true,OptimizerLoaded:true,SchedulerLoaded:true,RNGLoaded:true}
 if err:=ValidateEvidence(e,e.RunID,4,r);err!=nil{t.Fatal(err)}
 for _,which:=range []string{"model","optimizer","scheduler","rng","retrain","short","digest"}{t.Run(which,func(t *testing.T){bad:=e;switch which{case "model":bad.ModelLoaded=false;case "optimizer":bad.OptimizerLoaded=false;case "scheduler":bad.SchedulerLoaded=false;case "rng":bad.RNGLoaded=false;case "retrain":bad.StartStep=0;bad.ExecutedSteps=4;case "short":bad.FinalStep=3;case "digest":bad.SHA256=strings.Repeat("b",64)};if ValidateEvidence(bad,e.RunID,4,r)==nil{t.Fatal("invalid resumed evidence accepted")}})}
}
