// Package trainingoutput preserves completed compute separately from publication.
// Receipts contain execution credentials and must never be returned by public APIs.
package trainingoutput

import (
 "bytes"
 "encoding/json"
 "errors"
 "fmt"
 "io"
 "os"
 "path/filepath"
 "syscall"

 "github.com/google/uuid"
 "model-platform/internal/artifacts"
)

const ReceiptName = "completed.json"
const MaxReceipt = 20 << 20

type Receipt struct {
 Version int `json:"version"`
 RunID string `json:"training_run_id"`
 OperationID string `json:"operation_id"`
 DispatchToken string `json:"dispatch_token"`
 InstanceID string `json:"execution_instance_id"`
 Node string `json:"execution_node"`
 SourceID string `json:"source_model_version_id"`
 Manifest json.RawMessage `json:"manifest"`
 Digest string `json:"manifest_sha256"`
 Result json.RawMessage `json:"result"`
}

func validID(id string) bool { v,e:=uuid.Parse(id);return e==nil&&v!=uuid.Nil&&v.String()==id }

// Directory only accepts canonical identifiers from authoritative Operation data.
func Directory(root,runID,token string)(string,error){
 if !filepath.IsAbs(root)||filepath.Clean(root)!=root||!validID(runID)||!validID(token){return "",errors.New("invalid saved output location")}
 dir:=filepath.Join(root,runID,token)
 // Check each existing component, including the configured mount point.
 current:=string(filepath.Separator)
 for _,part:=range splitPath(dir){current=filepath.Join(current,part);info,e:=os.Lstat(current);if os.IsNotExist(e){continue};if e!=nil{return "",e};if !info.IsDir()||info.Mode()&os.ModeSymlink!=0{return "",errors.New("saved output path must contain real directories")}}
 return dir,nil
}
func splitPath(p string)[]string{var parts []string;for p!="/"&&p!="."{parts=append([]string{filepath.Base(p)},parts...);p=filepath.Dir(p)};return parts}

func (r Receipt) Validate() (artifacts.Manifest,error) {
 var empty artifacts.Manifest
 if r.Version!=1||!validID(r.RunID)||!validID(r.OperationID)||!validID(r.DispatchToken)||!validID(r.InstanceID)||!validID(r.SourceID){return empty,errors.New("invalid completed output identity")}
 manifest,digest,err:=artifacts.Parse(r.Manifest);if err!=nil{return empty,err};if digest!=r.Digest{return empty,errors.New("saved manifest digest mismatch")}
 var result struct{Exit *int `json:"train_exit_code"`;Source string `json:"source_model_version_id"`;Files int `json:"output_file_count"`;Bytes int64 `json:"output_bytes"`}
 if json.Unmarshal(r.Result,&result)!=nil||result.Exit==nil||*result.Exit!=0||result.Source!=r.SourceID||result.Files!=manifest.FileCount||result.Bytes!=manifest.TotalBytes{return empty,errors.New("completed compute evidence does not match manifest")}
 return manifest,nil
}

func Read(dir string)(Receipt,error){
 var r Receipt
 path:=filepath.Join(dir,ReceiptName);info,e:=os.Lstat(path);if e!=nil{return r,e};if !info.Mode().IsRegular()||info.Size()>MaxReceipt{return r,errors.New("invalid completed output receipt")}
 f,e:=os.Open(path);if e!=nil{return r,e};defer f.Close()
 dec:=json.NewDecoder(io.LimitReader(f,MaxReceipt+1));dec.DisallowUnknownFields()
 if e=dec.Decode(&r);e!=nil{return r,e};var extra any;if dec.Decode(&extra)!=io.EOF{return r,errors.New("trailing output receipt content")}
 _,e=r.Validate();return r,e
}

func Verify(dir string,r Receipt)error{
 if _,e:=r.Validate();e!=nil{return e}
 _,encoded,digest,e:=artifacts.Build(filepath.Join(dir,"output"));if e!=nil{return e}
 if digest!=r.Digest||!bytes.Equal(encoded,r.Manifest){return errors.New("retained output changed since compute completed")};return nil
}

// Seal syncs all output files before atomically making the completion receipt
// visible. A failed/terminated trainer never produces this proof.
func Seal(dir string,r Receipt)error{
 if e:=Verify(dir,r);e!=nil{return e}
 if old,e:=Read(dir);e==nil {a,_:=json.Marshal(old);b,_:=json.Marshal(r);if bytes.Equal(a,b){return nil};return errors.New("completed output is immutable")}else if !os.IsNotExist(e){return e}
 m,_:=r.Validate()
 dirs:=map[string]bool{filepath.Join(dir,"output"):true}
 for _,entry:=range m.Files{
  name:=filepath.Join(dir,"output",filepath.FromSlash(entry.Path));f,e:=os.Open(name);if e!=nil{return e};e=f.Sync();closeErr:=f.Close();if e!=nil{return e};if closeErr!=nil{return closeErr}
  for d:=filepath.Dir(name);d!=dir;d=filepath.Dir(d){dirs[d]=true}
 }
 for d:=range dirs{if e:=syncDir(d);e!=nil{return e}}
 encoded,e:=json.Marshal(r);if e!=nil{return e};if len(encoded)>MaxReceipt{return errors.New("output receipt exceeds size limit")}
 f,e:=os.CreateTemp(dir,".completed-*");if e!=nil{return e};name:=f.Name();defer os.Remove(name)
 if _,e=f.Write(encoded);e==nil{e=f.Sync()};closeErr:=f.Close();if e!=nil{return e};if closeErr!=nil{return closeErr}
 if e=os.Rename(name,filepath.Join(dir,ReceiptName));e!=nil{return e};if e=syncDir(dir);e!=nil{return e};if e=syncDir(filepath.Dir(dir));e!=nil{return e};return syncDir(filepath.Dir(filepath.Dir(dir)))
}
func syncDir(dir string)error{f,e:=os.Open(dir);if e!=nil{return e};defer f.Close();return f.Sync()}

// Lock serializes Job writes, publication workers and local cleanup even when a
// database connection is lost. Busy is a retryable observation, not permission
// to launch another training Job.
func Lock(dir string)(func(),error){
 path:=filepath.Join(dir,"output.lock")
 fd,e:=syscall.Open(path,syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW,0600);if e!=nil{return nil,e}
 f:=os.NewFile(uintptr(fd),path)
 if e=syscall.Flock(fd,syscall.LOCK_EX|syscall.LOCK_NB);e!=nil{f.Close();return nil,fmt.Errorf("saved output busy: %w",e)}
 return func(){_ = syscall.Flock(fd,syscall.LOCK_UN);_ = f.Close()},nil
}
