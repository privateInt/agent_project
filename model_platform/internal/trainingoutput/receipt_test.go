package trainingoutput

import (
 "encoding/json"
 "os"
 "path/filepath"
 "testing"
 "github.com/google/uuid"
 "model-platform/internal/artifacts"
)

func receiptFixture(t *testing.T)(string,Receipt){
 t.Helper();root:=t.TempDir();r:=Receipt{Version:1,RunID:uuid.NewString(),OperationID:uuid.NewString(),DispatchToken:uuid.NewString(),InstanceID:uuid.NewString(),SourceID:uuid.NewString()}
 dir,e:=Directory(root,r.RunID,r.DispatchToken);if e!=nil{t.Fatal(e)};if e=os.MkdirAll(filepath.Join(dir,"output"),0700);e!=nil{t.Fatal(e)}
 for name,data:=range map[string]string{"adapter_config.json":"{}","adapter_model.safetensors":"weights"}{if e=os.WriteFile(filepath.Join(dir,"output",name),[]byte(data),0600);e!=nil{t.Fatal(e)}}
 m,b,d,e:=artifacts.Build(filepath.Join(dir,"output"));if e!=nil{t.Fatal(e)};r.Manifest=b;r.Digest=d
 r.Result,_=json.Marshal(map[string]any{"train_exit_code":0,"source_model_version_id":r.SourceID,"output_file_count":m.FileCount,"output_bytes":m.TotalBytes})
 return dir,r
}
func TestCompletedReceiptSurvivesReloadAndDetectsMutation(t *testing.T){
 dir,r:=receiptFixture(t);if e:=Seal(dir,r);e!=nil{t.Fatal(e)};if e:=Seal(dir,r);e!=nil{t.Fatal("seal replay",e)}
 got,e:=Read(dir);if e!=nil||got.Digest!=r.Digest{t.Fatal(got,e)};if e=Verify(dir,got);e!=nil{t.Fatal(e)}
 if e=os.WriteFile(filepath.Join(dir,"output","adapter_model.safetensors"),[]byte("changed"),0600);e!=nil{t.Fatal(e)}
 if e=Verify(dir,got);e==nil{t.Fatal("changed output accepted")}
 if e=Seal(dir,got);e==nil{t.Fatal("changed output resealed")}
}
func TestIncompleteReceiptCannotAuthorizeRecovery(t *testing.T){
 for _,which:=range []string{"failed_compute","wrong_source","wrong_count","wrong_digest","bad_token","missing_weight","symlink"}{t.Run(which,func(t *testing.T){
  dir,r:=receiptFixture(t)
  switch which{
  case "failed_compute":r.Result=json.RawMessage(`{"train_exit_code":1}`)
  case "wrong_source":r.SourceID=uuid.NewString()
  case "wrong_count":r.Result=json.RawMessage(`{"train_exit_code":0}`)
  case "wrong_digest":r.Digest=artifacts.SHA256([]byte("other"))
  case "bad_token":r.DispatchToken="../escape"
  case "missing_weight":if e:=os.Remove(filepath.Join(dir,"output","adapter_model.safetensors"));e!=nil{t.Fatal(e)}
  case "symlink":if e:=os.Symlink("adapter_model.safetensors",filepath.Join(dir,"output","alias"));e!=nil{t.Fatal(e)}
  }
  if e:=Seal(dir,r);e==nil{t.Fatal("invalid completion sealed")};if _,e:=os.Stat(filepath.Join(dir,ReceiptName));!os.IsNotExist(e){t.Fatal("invalid output created receipt",e)}
 })}
}
func TestSavedOutputLockAndPathBoundaries(t *testing.T){
 dir,r:=receiptFixture(t);unlock,e:=Lock(dir);if e!=nil{t.Fatal(e)}
 if other,e:=Lock(dir);e==nil{other();t.Fatal("two writers entered")};unlock()
 unlock,e=Lock(dir);if e!=nil{t.Fatal(e)};unlock()
 root:=filepath.Dir(filepath.Dir(dir));if _,e=Directory(root,"../escape",r.DispatchToken);e==nil{t.Fatal("path traversal allowed")}
 link:=filepath.Join(t.TempDir(),"linked");if e=os.Symlink(root,link);e!=nil{t.Fatal(e)};if _,e=Directory(link,r.RunID,r.DispatchToken);e==nil{t.Fatal("symlink root allowed")}
}
