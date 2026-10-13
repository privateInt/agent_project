package jobcontrol

import (
 "bytes"
 "context"
 "encoding/binary"
 "encoding/json"
 "errors"
 "io"
 "net/http"
 "strings"
 "testing"
 operationv1 "model-platform/gen/operation/v1"
 "model-platform/internal/gpuadmission"
)

func frame(stream byte,text string)[]byte{h:=make([]byte,8);h[0]=stream;binary.BigEndian.PutUint32(h[4:],uint32(len(text)));return append(h,[]byte(text)...)}
func TestDockerLogsDemultiplexAndBounds(t *testing.T){
 raw:=append(frame(1,"hello\n"),frame(2,"first\nsecond\nthird\n")...)
 stdout,stderr:=&tailBuffer{max:64},&tailBuffer{max:14}
 if err:=readMultiplex(bytes.NewReader(raw),stdout,stderr);err!=nil{t.Fatal(err)}
 if string(stdout.lines(1))!="hello\n"||string(stderr.lines(2))!="second\nthird\n"||!stderr.clipped{t.Fatal(stdout,stderr)}
 for _,bad:=range [][]byte{{1,0,0},append([]byte{3},make([]byte,7)...),frame(1,"lost")[:10]}{
  if readMultiplex(bytes.NewReader(bad),io.Discard,io.Discard)==nil{t.Fatal("corrupt stream accepted")}
 }
}
type roundTrip func(*http.Request)(*http.Response,error)
func(f roundTrip)RoundTrip(r *http.Request)(*http.Response,error){return f(r)}
func response(code int,raw []byte)*http.Response{return &http.Response{StatusCode:code,Body:io.NopCloser(bytes.NewReader(raw)),Header:make(http.Header)}}
func testOperation()*operationv1.Operation{return &operationv1.Operation{OperationId:"11111111-1111-4111-8111-111111111111",ResourceId:"22222222-2222-4222-8222-222222222222",DispatchToken:"33333333-3333-4333-8333-333333333333",ExecutionInstanceId:"44444444-4444-4444-8444-444444444444",ResourceType:"training_run",ExecutionType:"training.run",DispatchProtocol:1,Status:"cancelling"}}
func testInspection(op *operationv1.Operation)inspection{
 c:=inspection{ID:strings.Repeat("a",64),Name:"/model-op-"+op.OperationId+"-"+op.DispatchToken}
 c.Config.Labels=map[string]string{"model-platform.owner":"operation-dispatcher","model-platform.operation_id":op.OperationId,"model-platform.execution_type":"training.run","model-platform.dispatch_token":op.DispatchToken}
 c.State.Status="running";c.State.Running=true;return c
}
func TestCancelVerifiesOwnershipAndConfirmsStop(t *testing.T){
 for _,foreign:=range []bool{false,true}{t.Run(map[bool]string{false:"owned",true:"foreign"}[foreign],func(t *testing.T){
  op:=testOperation();c:=testInspection(op);if foreign{c.Config.Labels["model-platform.owner"]="other"}
  stops:=0
  runtime:=&Runtime{Guard:gpuadmission.Guard{Dir:t.TempDir(),Inventory:func(context.Context)([]gpuadmission.Container,error){
   item:=gpuadmission.Container{ID:c.ID,Name:c.Name};item.Config.Labels=c.Config.Labels;item.State.Status=c.State.Status;item.State.Running=c.State.Running;return []gpuadmission.Container{item},nil
  }}}
  runtime.HTTP=&http.Client{Transport:roundTrip(func(req *http.Request)(*http.Response,error){
   if req.Method=="GET"{raw,_:=json.Marshal(c);return response(200,raw),nil}
   if req.Method!="POST"||req.URL.Path!="/v1.45/containers/"+c.ID+"/stop"||req.URL.Query().Get("t")!="10"{t.Fatal("unexpected mutation",req.Method,req.URL)}
   stops++;c.State.Status="exited";c.State.Running=false;return response(204,nil),nil
  })}
  err:=runtime.Cancel(context.Background(),op)
  if foreign{if err==nil||stops!=0{t.Fatal("foreign container stopped",err)}}else{if err!=nil||stops!=1{t.Fatal(err,stops)}}
 })}
}
type memoryArchive map[string][]byte
func(m memoryArchive)Open(_ context.Context,key string)(io.ReadCloser,error){b,ok:=m[key];if !ok{return nil,errors.New("missing")};return io.NopCloser(bytes.NewReader(b)),nil}
func(m memoryArchive)Manifests(_ context.Context,prefix string)([]string,error){var out []string;for key:=range m{if strings.HasPrefix(key,prefix)&&strings.HasSuffix(key,"/manifest.json"){out=append(out,key)}};return out,nil}
func archiveFixture(op *operationv1.Operation)memoryArchive{
 id:=strings.Repeat("a",64)
 meta,_:=json.Marshal(map[string]any{"schema_version":1,"operation_id":op.OperationId,"dispatch_token":op.DispatchToken,"execution_instance_id":op.ExecutionInstanceId,"resource_id":op.ResourceId,"resource_type":"training_run","execution_type":"training.run","container_id":id,"status":op.Status})
 data:=map[string][]byte{"metadata.json":meta,"stdout.log":[]byte("one\ntwo\nthree\n"),"stderr.log":[]byte("warning\n")}
 var files []entry;for _,name:=range []string{"metadata.json","stderr.log","stdout.log"}{files=append(files,entry{name,int64(len(data[name])),hash(data[name])})}
 manifest,_:=json.Marshal(map[string]any{"schema_version":1,"kind":"job-logs","files":files})
 prefix:="operations/"+op.OperationId+"/"+op.DispatchToken+"/"+id+"/"+hash(manifest)+"/"
 out:=memoryArchive{prefix+"manifest.json":manifest};for name,raw:=range data{out[prefix+name]=raw};return out
}
func TestArchivedLogsVerifyManifestMetadataAndContent(t *testing.T){
 for _,corrupt:=range []string{"","stdout.log","metadata.json","manifest.json"}{t.Run("corrupt-"+corrupt,func(t *testing.T){
  op:=testOperation();op.Status="cancelled";store:=archiveFixture(op)
  if corrupt!=""{for key:=range store{if strings.HasSuffix(key,"/"+corrupt){store[key]=append(store[key],byte('x'))}}}
  out,err:=(&ArchiveReader{Store:store}).Read(context.Background(),op,2,1024)
  if corrupt!=""{if err==nil{t.Fatal("corrupt archive accepted")};return}
  if err!=nil||out.GetSource()!="archive"||out.GetStdout()!="two\nthree\n"||out.GetStderr()!="warning\n"{t.Fatal(out,err)}
  op.ResourceId="55555555-5555-4555-8555-555555555555"
  if _,err=(&ArchiveReader{Store:store}).Read(context.Background(),op,2,1024);err==nil{t.Fatal("foreign training metadata accepted")}
 })}
}
func TestLogAPIUsesImmutableIDAndArchiveAfterGCRace(t *testing.T){
 op:=testOperation();op.Status="cancelled";c:=testInspection(op)
 runtime:=&Runtime{Archive:&ArchiveReader{Store:archiveFixture(op)},HTTP:&http.Client{Transport:roundTrip(func(req *http.Request)(*http.Response,error){
  if strings.HasSuffix(req.URL.Path,"/json"){b,_:=json.Marshal(c);return response(200,b),nil}
  if req.URL.Path!="/v1.45/containers/"+c.ID+"/logs"{t.Fatal("logs used mutable name",req.URL.Path)}
  return response(404,nil),nil
 })}}
 out,err:=runtime.Logs(context.Background(),op,2,1024);if err!=nil||out.GetSource()!="archive"{t.Fatal(out,err)}
}
