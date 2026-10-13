package creation_test

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"mime/multipart"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/datasets"

	"github.com/gofiber/fiber/v3"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const datasetLine = `{"messages":[{"role":"user","content":"question"},{"role":"assistant","content":"answer"}]}`+"\n"
const datasetContent = datasetLine+datasetLine

type datasetMemoryStore struct {
	mu sync.Mutex
	objects map[string][]byte
	puts int
	failPut, corruptRead bool
}
func (m *datasetMemoryStore) URI(key string) string {return "s3://test-datasets/"+key}
func (m *datasetMemoryStore) Put(ctx context.Context,key string,r io.Reader,size int64,kind string)error {
	m.mu.Lock();defer m.mu.Unlock()
	if m.failPut {return errors.New("test: storage offline")}
	data,err:=io.ReadAll(r);if err!=nil{return err}
	if int64(len(data))!=size{return errors.New("test: wrong upload size")}
	if m.objects==nil{m.objects=map[string][]byte{}}
	m.objects[key]=append([]byte(nil),data...);m.puts++
	return nil
}
func (m *datasetMemoryStore) OpenObject(ctx context.Context,key string)(io.ReadCloser,error){
	m.mu.Lock();defer m.mu.Unlock()
	data,ok:=m.objects[key];if !ok{return nil,errors.New("test: missing object")}
	data=append([]byte(nil),data...)
	if m.corruptRead {data=append(data,' ')}
	return io.NopCloser(bytes.NewReader(data)),nil
}

func (f *fixture) uploadDataset(t *testing.T,id,label,content,key string)(int,map[string]any){
	t.Helper()
	var body bytes.Buffer
	w:=multipart.NewWriter(&body)
	if err:=w.WriteField("version",label);err!=nil{t.Fatal(err)}
	file,err:=w.CreateFormFile("file","train.jsonl");if err!=nil{t.Fatal(err)}
	if _,err:=io.WriteString(file,content);err!=nil{t.Fatal(err)}
	if err:=w.Close();err!=nil{t.Fatal(err)}
	req:=httptest.NewRequest("POST","/api/datasets/"+id+"/versions",&body)
	req.Header.Set("Content-Type",w.FormDataContentType());if key!=""{req.Header.Set("Idempotency-Key",key)}
	res,err:=f.app.Test(req,fiber.TestConfig{Timeout:25*time.Second,FailOnTimeout:true});if err!=nil{t.Fatal(err)}
	defer res.Body.Close()
	var result map[string]any
	if err:=json.NewDecoder(res.Body).Decode(&result);err!=nil{t.Fatal(err)}
	return res.StatusCode,result
}
func (f *fixture) createDataset(t *testing.T)string{
	t.Helper();code,res:=f.request(t,"POST","/api/datasets",`{"name":"fixture"}`,"dataset-create")
	if code!=201{t.Fatalf("create: %d %v",code,res)}
	return res["dataset_id"].(string)
}

func TestDatasetRegistrationReplayAndImmutability(t *testing.T){
	f:=newFixture(t);store:=&datasetMemoryStore{};f.trainer.ConfigureDatasets(store)
	id:=f.createDataset(t)
	code,replay:=f.request(t,"POST","/api/datasets",`{"name":" fixture ","description":""}`,"dataset-create")
	if code!=201 || replay["dataset_id"]!=id{t.Fatal("dataset create replay failed")}
	if code,_:=f.request(t,"POST","/api/datasets",`{"name":"different"}`,"dataset-create");code!=409{t.Fatal("dataset key conflict accepted")}
	code,first:=f.uploadDataset(t,id,"v1",datasetContent,"version-upload");if code!=201{t.Fatalf("upload: %d %v",code,first)}
	info,err:=datasets.Inspect(strings.NewReader(datasetContent));if err!=nil{t.Fatal(err)}
	if first["sha256"]!=info.SHA256 || first["record_count"]!=float64(2) || first["size_bytes"]!=float64(len(datasetContent)){t.Fatal("wrong dataset metadata")}
	code,got:=f.request(t,"GET","/api/dataset-versions/"+first["dataset_version_id"].(string),"")
	if code!=200 || !reflect.DeepEqual(first,got){t.Fatal("version lookup changed response")}
	store.failPut=true // A committed replay must not require another object write.
	for _,key:=range []string{"version-upload","another-key",""}{
		code,res:=f.uploadDataset(t,id," v1 ",datasetContent,key)
		if code!=201 || !reflect.DeepEqual(first,res){t.Fatalf("upload replay: %d %v",code,res)}
	}
	if store.puts!=1{t.Fatal("replay rewrote content")}
	for _,key:=range []string{"version-upload","different-key",""}{
		if code,_:=f.uploadDataset(t,id,"v1",datasetContent+"\n",key);code!=409{t.Fatal("immutable label replaced")}
	}
	store.failPut=false
	if code,_:=f.uploadDataset(t,id,"v2",datasetContent,"version-upload");code!=409{t.Fatal("one key created a different version")}
	if count(t,f.training,`SELECT count(*) FROM dataset_versions`)!=1{t.Fatal("duplicate version committed")}
	code,list:=f.request(t,"GET","/api/datasets/"+id+"/versions?limit=1","")
	if code!=200 || len(list["versions"].([]any))!=1 || list["next_after_id"]!=""{t.Fatalf("list versions: %d %v",code,list)}
	if count(t,f.ops,`SELECT count(*) FROM operations`)!=0{t.Fatal("registration unexpectedly scheduled a Job")}
}

func TestDatasetUploadFailureAndRollback(t *testing.T){
	f:=newFixture(t);store:=&datasetMemoryStore{};f.trainer.ConfigureDatasets(store);id:=f.createDataset(t)
	for name,content:=range map[string]string{"empty":"","one-record":datasetLine,"malformed-late":strings.Repeat(datasetLine,20)+"not json\n"}{
		t.Run(name,func(t *testing.T){if code,_:=f.uploadDataset(t,id,"invalid",content,"invalid");code!=400{t.Fatalf("invalid upload returned %d",code)}})
	}
	if store.puts!=0{t.Fatal("invalid dataset reached storage")}
	if code,_:=f.uploadDataset(t,uuid.NewString(),"v1",datasetContent,"");code!=404{t.Fatal("missing dataset accepted")}
	store.failPut=true
	if code,_:=f.uploadDataset(t,id,"v1",datasetContent,"retry");code!=503{t.Fatal("offline upload accepted")}
	store.failPut=false;store.corruptRead=true
	if code,_:=f.uploadDataset(t,id,"v1",datasetContent,"retry");code!=503{t.Fatal("corrupt readback accepted")}
	store.corruptRead=false
	if count(t,f.training,`SELECT count(*) FROM dataset_versions`)!=0 || count(t,f.training,`SELECT count(*) FROM api_requests WHERE scope='dataset.version.upload.v1'`)!=0{t.Fatal("failed upload leaked ready version or key")}
	execSQL(t,f.training,`CREATE FUNCTION reject_dataset_version() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'test rollback'; END $$; CREATE TRIGGER reject_dataset_version BEFORE INSERT ON dataset_versions FOR EACH ROW EXECUTE FUNCTION reject_dataset_version()`)
	if code,_:=f.uploadDataset(t,id,"v1",datasetContent,"retry");code!=500{t.Fatal("DB fault ignored")}
	if count(t,f.training,`SELECT count(*) FROM api_requests WHERE scope='dataset.version.upload.v1'`)!=0{t.Fatal("DB rollback retained key")}
	execSQL(t,f.training,`DROP TRIGGER reject_dataset_version ON dataset_versions`)
	if code,res:=f.uploadDataset(t,id,"v1",datasetContent,"retry");code!=201{t.Fatalf("recovery: %d %v",code,res)}
}

func TestDatasetConcurrentVersionLabel(t *testing.T){
	for _,different:=range []bool{false,true}{t.Run(fmt.Sprint(different),func(t *testing.T){
		f:=newFixture(t);store:=&datasetMemoryStore{};f.trainer.ConfigureDatasets(store);id:=f.createDataset(t)
		var wg sync.WaitGroup
		results:=make(chan int,2)
		for i:=0;i<2;i++{wg.Add(1);go func(i int){defer wg.Done();body:=datasetContent;if different && i==1{body+="\n"};code,_:=f.uploadDataset(t,id,"race",body,fmt.Sprintf("race-%d",i));results<-code}(i)}
		wg.Wait();close(results);success,conflict:=0,0
		for code:=range results{switch code{case 201:success++;case 409:conflict++;default:t.Fatalf("unexpected status: %d",code)}}
		if different && (success!=1 || conflict!=1){t.Fatal("conflicting content did not serialize")}
		if !different && success!=2{t.Fatal("identical content did not converge")}
		if count(t,f.training,`SELECT count(*) FROM dataset_versions`)!=1{t.Fatal("race created duplicate versions")}
	})}
}

func TestDatasetTrainingPinsVersionAndReplays(t *testing.T){
	f:=newFixture(t);store:=&datasetMemoryStore{};f.trainer.ConfigureDatasets(store);id:=f.createDataset(t)
	code,v1:=f.uploadDataset(t,id,"v1",datasetContent,"v1");if code!=201{t.Fatal(v1)}
	body:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q}}`,f.versionID,v1["dataset_version_id"])
	code,first:=f.request(t,"POST","/api/training-runs",body,"train-version");if code!=202{t.Fatalf("start: %d %v",code,first)}
	code,run:=f.request(t,"GET","/api/training-runs/"+first["training_run_id"].(string),"");if code!=200{t.Fatal(run)}
	pin:=run["dataset"].(map[string]any)
	if pin["dataset_version_id"]!=v1["dataset_version_id"] || pin["source_uri"]!=v1["artifact_uri"] || pin["sha256"]!=v1["sha256"] || pin["size_bytes"]!=v1["size_bytes"] || pin["record_count"]!=v1["record_count"]{t.Fatalf("run missing pin: %v",pin)}
	if code,_:=f.uploadDataset(t,id,"v2",datasetContent+"\n","v2");code!=201{t.Fatal("v2 upload failed")}
	f.managementOffline.Store(true)
	code,replay:=f.request(t,"POST","/api/training-runs",body,"train-version")
	if code!=202 || !reflect.DeepEqual(first,replay){t.Fatalf("run replay needs external lookup: %d %v",code,replay)}
	f.managementOffline.Store(false)
	code,unchanged:=f.request(t,"GET","/api/training-runs/"+first["training_run_id"].(string),"")
	if code!=200 || !reflect.DeepEqual(pin,unchanged["dataset"]){t.Fatal("new version changed stored run")}
	missing:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q}}`,f.versionID,uuid.NewString())
	if code,_:=f.request(t,"POST","/api/training-runs",missing,"missing-dataset");code!=404{t.Fatal("missing version scheduled training")}
	for _,field:=range []string{`"source_uri":"s3://override/train.jsonl"`,`"sha256":"fake"`,`"size_bytes":1`,`"record_count":2`}{
		spoofed:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q,%s}}`,f.versionID,v1["dataset_version_id"],field)
		if code,_:=f.request(t,"POST","/api/training-runs",spoofed,"spoof");code!=400{t.Fatal("client overrode pin")}
	}
	if count(t,f.training,`SELECT count(*) FROM training_runs`)!=1 || count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("invalid or replayed request created extra work")}
}

func TestDatasetUploadStreamProtocol(t *testing.T){
	f:=newFixture(t);f.trainer.ConfigureDatasets(&datasetMemoryStore{});id:=f.createDataset(t)
	header:=&trainingv1.UploadDatasetVersionRequest{Payload:&trainingv1.UploadDatasetVersionRequest_Header{Header:&trainingv1.DatasetUploadHeader{DatasetId:id,Version:"v1"}}}
	chunk:=func(b []byte)*trainingv1.UploadDatasetVersionRequest{return &trainingv1.UploadDatasetVersionRequest{Payload:&trainingv1.UploadDatasetVersionRequest_Chunk{Chunk:b}}}
	for name,messages:=range map[string][]*trainingv1.UploadDatasetVersionRequest{
		"no-header":{},"chunk-first":{chunk([]byte(datasetContent))},"duplicate-header":{header,header},"empty-chunk":{header,chunk(nil)},"oversized-chunk":{header,chunk(make([]byte,datasets.ChunkBytes+1))},
	}{t.Run(name,func(t *testing.T){
		ctx,cancel:=context.WithTimeout(context.Background(),10*time.Second);defer cancel()
		stream,err:=f.clients.Training.UploadDatasetVersion(ctx);if err!=nil{t.Fatal(err)}
		for _,message:=range messages{if err:=stream.Send(message);err!=nil{break}}
		if _,err:=stream.CloseAndRecv();status.Code(err)!=codes.InvalidArgument{t.Fatalf("expected invalid protocol, got %v",err)}
	})}
	if count(t,f.training,`SELECT count(*) FROM dataset_versions`)!=0{t.Fatal("invalid stream committed a version")}
}

func TestDatasetPagination(t *testing.T){
	f:=newFixture(t)
	for i:=0;i<3;i++{if code,_:=f.request(t,"POST","/api/datasets",fmt.Sprintf(`{"name":"page-%d"}`,i));code!=201{t.Fatal("create")}}
	seen:=map[string]bool{};after:=""
	for i:=0;i<3;i++{
		code,res:=f.request(t,"GET","/api/datasets?limit=1&after_id="+after,"");if code!=200{t.Fatal(res)}
		items:=res["datasets"].([]any);if len(items)!=1{t.Fatal("wrong page size")}
		id:=items[0].(map[string]any)["dataset_id"].(string);if seen[id]{t.Fatal("duplicate page")};seen[id]=true
		after=res["next_after_id"].(string)
		if (i<2 && after=="") || (i==2 && after!=""){t.Fatal("wrong continuation cursor")}
	}
	for _,query:=range []string{"limit=-1","limit=101","limit=bad","after_id=bad"}{if code,_:=f.request(t,"GET","/api/datasets?"+query,"");code!=400{t.Fatal("invalid page accepted")}}
}

func TestDatasetUploadBeyondUnaryMessageLimit(t *testing.T){
	f:=newFixture(t);f.trainer.ConfigureDatasets(&datasetMemoryStore{});id:=f.createDataset(t)
	content:=strings.Repeat(datasetLine,(5<<20)/len(datasetLine)+1)
	code,result:=f.uploadDataset(t,id,"large",content,"large-upload")
	if code!=201 || result["size_bytes"]!=float64(len(content)){t.Fatalf("multipart / gRPC streaming failed: %d %v",code,result)}
}

func TestDatasetCompletionRequiresMatchingPin(t *testing.T){
	f:=newFixture(t);f.trainer.ConfigureDatasets(&datasetMemoryStore{});id:=f.createDataset(t)
	code,v:=f.uploadDataset(t,id,"v1",datasetContent,"v1");if code!=201{t.Fatal(v)}
	body:=fmt.Sprintf(`{"source_model_version_id":%q,"dataset":{"source_type":"version","dataset_version_id":%q}}`,f.versionID,v["dataset_version_id"])
	code,created:=f.request(t,"POST","/api/training-runs",body,"complete-pin");if code!=202{t.Fatal(created)}
	runID,outputID:=created["training_run_id"].(string),uuid.NewString()
	uri,hash:="s3://test/output/",strings.Repeat("a",64)
	execSQL(t,f.management,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind,training_run_id,source_model_version_id,artifact_uri,artifact_sha256,artifact_size_bytes,artifact_manifest_sha256,artifact_file_count) VALUES($1,$2,'output','ready','training','adapter',$3,$4,$5,$6,10,$6,2)`,outputID,f.modelID,runID,f.versionID,uri,hash)
	result:=map[string]any{"train_exit_code":0,"output_file_count":2,"output_model_version_id":outputID,"output_artifact_uri":uri,"output_manifest_sha256":hash}
	complete:=func()error{
		raw,err:=json.Marshal(result);if err!=nil{t.Fatal(err)}
		_,err=f.clients.Training.CompleteTraining(context.Background(),&trainingv1.CompleteTrainingRequest{TrainingRunId:runID,OutputModelVersionId:outputID,ResultJson:string(raw)})
		return err
	}
	if err:=complete();status.Code(err)!=codes.FailedPrecondition{t.Fatalf("unpinned result accepted: %v",err)}
	result["dataset_version_id"],result["dataset_sha256"],result["dataset_bytes"],result["dataset_record_count"]=v["dataset_version_id"],v["sha256"],v["size_bytes"],v["record_count"]
	result["dataset_source_type"],result["dataset_source_uri"],result["dataset_format"]="version",v["artifact_uri"],"chat_messages"
	for _,field:=range []string{"dataset_version_id","dataset_sha256","dataset_source_uri","dataset_format"}{
		old:=result[field];result[field]="wrong"
		if err:=complete();status.Code(err)!=codes.FailedPrecondition{t.Fatalf("mismatched %s accepted: %v",field,err)}
		result[field]=old
	}
	if count(t,f.training,`SELECT count(*) FROM training_runs WHERE status='succeeded'`)!=0{t.Fatal("invalid result changed state")}
	if err:=complete();err!=nil{t.Fatal(err)}
	if err:=complete();err!=nil{t.Fatalf("completion replay: %v",err)}
}
