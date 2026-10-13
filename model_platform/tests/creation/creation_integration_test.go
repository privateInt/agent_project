package creation_test

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	deploymentapp "model-platform/deployment/app"
	deploymentclient "model-platform/deployment/grpcclient"
	gatewayapp "model-platform/gateway/app"
	gatewayclient "model-platform/gateway/grpcclient"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/storage"
	"model-platform/internal/datasets"
	"model-platform/internal/submission"
	"model-platform/internal/testsupport"
	managementapp "model-platform/management/app"
	managementclient "model-platform/management/grpcclient"
	operationapp "model-platform/operation/app"
	trainingapp "model-platform/training/app"
	trainingclient "model-platform/training/grpcclient"

	"github.com/gofiber/fiber/v3"
	"github.com/google/uuid"
	"github.com/jackc/pgx/v5/pgxpool"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type fixture struct {
	app *fiber.App
	ops, management, training, deployment *pgxpool.Pool
	mgmt *managementapp.Service
	trainer *trainingapp.Service
	deploy *deploymentapp.Service
	clients *gatewayclient.Clients
	modelID, versionID string
	failEnsure atomic.Int32
	lostReply atomic.Bool
	managementOffline atomic.Bool
	legacyCalls atomic.Int32
}

func serve(t *testing.T, interceptor grpc.UnaryServerInterceptor, register func(*grpc.Server)) string {
	t.Helper()
	listener,err:=net.Listen("tcp","127.0.0.1:0"); if err!=nil {t.Fatal(err)}
	opts:=[]grpc.ServerOption{}
	if interceptor!=nil {opts=append(opts,grpc.UnaryInterceptor(interceptor))}
	server:=grpc.NewServer(opts...); register(server)
	go func(){_ = server.Serve(listener)}()
	t.Cleanup(func(){server.Stop();_ = listener.Close()})
	return listener.Addr().String()
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	f:=&fixture{ops:testsupport.Postgres(t),management:testsupport.Postgres(t),training:testsupport.Postgres(t),deployment:testsupport.Postgres(t),modelID:uuid.NewString(),versionID:uuid.NewString()}
	ctx:=context.Background()
	for _, step:=range []struct{pool *pgxpool.Pool; init func(context.Context,*pgxpool.Pool)error}{
		{f.ops,operationapp.InitSchema},{f.management,managementapp.InitSchema},{f.training,trainingapp.InitSchema},{f.deployment,deploymentapp.InitSchema},
	}{if err:=step.init(ctx,step.pool);err!=nil {t.Fatal(err)}}
	operationAddress:=serve(t,func(ctx context.Context,req any,info *grpc.UnaryServerInfo,handler grpc.UnaryHandler)(any,error){
		if strings.HasSuffix(info.FullMethod,"/CreateOperation") {f.legacyCalls.Add(1)}
		if strings.HasSuffix(info.FullMethod,"/EnsureOperation") {
			if code:=codes.Code(f.failEnsure.Load());code!=codes.OK{return nil,status.Error(code,"test fault")}
			response,err:=handler(ctx,req)
			if err==nil && f.lostReply.CompareAndSwap(true,false) {return nil,status.Error(codes.Unavailable,"test: committed response lost")}
			return response,err
		}
		return handler(ctx,req)
	},func(s *grpc.Server){operationv1.RegisterOperationServiceServer(s,operationapp.New(f.ops))})
	ops,err:=managementclient.ConnectOperation(operationAddress);if err!=nil{t.Fatal(err)};t.Cleanup(func(){_ = ops.Close()})
	f.mgmt=managementapp.New(f.management,ops,bytes.Repeat([]byte{7},32),&storage.MinIO{})
	managementAddress:=serve(t,func(ctx context.Context,req any,info *grpc.UnaryServerInfo,handler grpc.UnaryHandler)(any,error){
		if f.managementOffline.Load() && strings.HasSuffix(info.FullMethod,"/GetModelVersion") {return nil,status.Error(codes.Unavailable,"test: model lookup offline")}
		return handler(ctx,req)
	},func(s *grpc.Server){managementv1.RegisterManagementServiceServer(s,f.mgmt)})
	tc,err:=trainingclient.Connect(managementAddress,operationAddress);if err!=nil{t.Fatal(err)};t.Cleanup(tc.Close)
	f.trainer=trainingapp.New(f.training,tc)
	trainingAddress:=serve(t,nil,func(s *grpc.Server){trainingv1.RegisterTrainingServiceServer(s,f.trainer)})
	dc,err:=deploymentclient.Connect(managementAddress,operationAddress);if err!=nil{t.Fatal(err)};t.Cleanup(dc.Close)
	f.deploy=deploymentapp.New(f.deployment,dc)
	deploymentAddress:=serve(t,nil,func(s *grpc.Server){deploymentv1.RegisterDeploymentServiceServer(s,f.deploy)})
	f.clients,err=gatewayclient.Connect(managementAddress,operationAddress,trainingAddress,deploymentAddress);if err!=nil{t.Fatal(err)};t.Cleanup(f.clients.Close)
	f.app=fiber.New(fiber.Config{BodyLimit:datasets.MaxBytes+(1<<20)});gatewayapp.New(f.clients).Register(f.app)
	// Ready model metadata only: no MinIO object, model import or GPU execution.
	if _,err=f.management.Exec(ctx,`INSERT INTO models(model_id,name,model_type) VALUES($1,$2,'llm')`,f.modelID,"source-"+f.modelID);err!=nil{t.Fatal(err)}
	if _,err=f.management.Exec(ctx,`INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,artifact_kind,repo_id,artifact_uri,artifact_sha256,artifact_size_bytes) VALUES($1,$2,'v1','ready','huggingface','snapshot','fixture/model','s3://test/source/', $3,1)`,f.versionID,f.modelID,strings.Repeat("a",64));err!=nil{t.Fatal(err)}
	return f
}

func (f *fixture) request(t *testing.T, method,path,body string,keys ...string)(int,map[string]any){
	t.Helper()
	req:=httptest.NewRequest(method,path,strings.NewReader(body));req.Header.Set("Content-Type","application/json")
	for _,key:=range keys{req.Header.Add("Idempotency-Key",key)}
	response,err:=f.app.Test(req,fiber.TestConfig{Timeout:25*time.Second,FailOnTimeout:true});if err!=nil{t.Fatal(err)}
	defer response.Body.Close(); data,err:=io.ReadAll(response.Body);if err!=nil{t.Fatal(err)}
	result:=map[string]any{};if err:=json.Unmarshal(data,&result);err!=nil{t.Fatalf("invalid JSON %s: %v",data,err)}
	return response.StatusCode,result
}
func count(t *testing.T,pool *pgxpool.Pool,sql string,args ...any)int{t.Helper();var n int;if err:=pool.QueryRow(context.Background(),sql,args...).Scan(&n);err!=nil{t.Fatal(err)};return n}
func execSQL(t *testing.T,pool *pgxpool.Pool,sql string,args ...any){t.Helper();if _,err:=pool.Exec(context.Background(),sql,args...);err!=nil{t.Fatal(err)}}
func (f *fixture) due(t *testing.T,pool *pgxpool.Pool){execSQL(t,pool,`UPDATE operation_submissions SET next_attempt_at=clock_timestamp() WHERE status='pending'`)}
func (f *fixture) worker(pool *pgxpool.Pool)submission.Worker{return submission.Worker{Pool:pool,Ensure:func(ctx context.Context,req *operationv1.CreateOperationRequest)(*operationv1.Operation,error){return f.clients.Operation.EnsureOperation(ctx,req)}}}
func (f *fixture) deploymentBody()string{return fmt.Sprintf(`{"model_version_id":%q}`,f.versionID)}

type routeCase struct{name,path,body,equivalent,changed,idField string;code int}
func (f *fixture) routes()[]routeCase{return []routeCase{
	{"model","/api/models",`{"name":"idem-model"}`,`{"name":" idem-model ","model_type":"LLM","description":""}`,`{"name":"idem-model","description":"changed"}`,"model_id",201},
	{"import","/api/models/"+f.modelID+"/imports/huggingface",`{"version":"new","repo_id":"fixture/import","hf_token":"test-private-token"}`,`{"revision":"main","repo_id":"fixture/import","version":"new","hf_token":"test-private-token"}`,`{"version":"new","repo_id":"fixture/import","hf_token":"changed-token"}`,"model_version_id",202},
	{"training","/api/training-runs",fmt.Sprintf(`{"source_model_version_id":%q}`,f.versionID),fmt.Sprintf(`{"training":{"method":"qlora"},"resources":{"gpu_count":1},"source_model_version_id":%q}`,f.versionID),fmt.Sprintf(`{"source_model_version_id":%q,"training":{"max_steps":2}}`,f.versionID),"training_run_id",202},
	{"deployment","/api/deployments",f.deploymentBody(),fmt.Sprintf(`{"resources":{"gpu_count":0},"model_version_id":%q}`,f.versionID),fmt.Sprintf(`{"model_version_id":%q,"resources":{"gpu_ids":[0]}}`,f.versionID),"deployment_id",202},
}}

func TestRESTCreationReplayAndConflict(t *testing.T){
	f:=newFixture(t)
	for _,tc:=range f.routes(){t.Run(tc.name,func(t *testing.T){
		code,first:=f.request(t,"POST",tc.path,tc.body,"same-key-across-routes")
		if code!=tc.code {t.Fatalf("create %d: %v",code,first)}
		code,again:=f.request(t,"POST",tc.path,tc.equivalent,"same-key-across-routes")
		if code!=tc.code || !reflect.DeepEqual(first,again){t.Fatalf("replay changed %d: %v vs %v",code,first,again)}
		code,_=f.request(t,"POST",tc.path,tc.changed,"same-key-across-routes");if code!=409{t.Fatalf("conflict returned %d",code)}
		if tc.name != "model" {
			path := map[string]string{"import": "/api/model-versions/", "training": "/api/training-runs/", "deployment": "/api/deployments/"}[tc.name]
			code, view := f.request(t, "GET", path+first[tc.idField].(string), "")
			if code != 200 || view["operation_id"] != first["operation_id"] || view["submission_status"] != "delivered" {
				t.Fatalf("domain submission lookup: %d %v", code, view)
			}
		}
	})}
	if n:=count(t,f.ops,`SELECT count(*) FROM operations`);n!=3{t.Fatalf("duplicate operations: %d",n)}
	if n:=count(t,f.ops,`SELECT count(*) FROM operation_outbox`);n!=3{t.Fatalf("duplicate events: %d",n)}
	if f.legacyCalls.Load()!=0{t.Fatal("domain fell back to non-idempotent legacy RPC")}
}

func TestRESTConcurrentDuplicatesAndHeaderValidation(t *testing.T){
	f:=newFixture(t)
	// Initialize Fiber's route tree before concurrent requests.
	f.request(t,"GET","/health","")
	var wg sync.WaitGroup; results:=make(chan map[string]any,8)
	for i:=0;i<8;i++{wg.Add(1);go func(){defer wg.Done();code,res:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"race-key");if code!=202{t.Errorf("concurrent status %d: %v",code,res)};results<-res}()}
	wg.Wait();close(results);var first map[string]any
	for res:=range results{if first==nil{first=res}else if !reflect.DeepEqual(first,res){t.Fatal("concurrent response mismatch")}}
	if count(t,f.deployment,`SELECT count(*) FROM deployments`)!=1 || count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("duplicate domain or event")}
	for _,keys:=range [][]string{{""},{"a","b"},{"a b"},{strings.Repeat("x",129)}}{code,_:=f.request(t,"POST","/api/deployments",f.deploymentBody(),keys...);if code!=400{t.Fatalf("bad key accepted: %d",code)}}
	code,a:=f.request(t,"POST","/api/deployments",f.deploymentBody());if code!=202{t.Fatal(code)}
	code,b:=f.request(t,"POST","/api/deployments",f.deploymentBody());if code!=202 || a["deployment_id"]==b["deployment_id"]{t.Fatal("missing key changed legacy behavior")}
}

func TestAcceptedRequestSurvivesOperationOutageAndWorkerRestart(t *testing.T){
	f:=newFixture(t);f.failEnsure.Store(int32(codes.Unavailable))
	code,created:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"offline-key");if code!=202{t.Fatal(code,created)}
	code,resource:=f.request(t,"GET","/api/deployments/"+created["deployment_id"].(string),"")
	if code!=200 || resource["status"]!="created" || resource["submission_status"]!="pending" || resource["operation_id"]!=created["operation_id"]{t.Fatal("domain incorrectly failed",resource)}
	code,_=f.request(t,"GET","/api/operations/"+created["operation_id"].(string),"");if code!=404{t.Fatal("unexpected operation before delivery",code)}
	f.managementOffline.Store(true)
	code,again:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"offline-key");if code!=202 || !reflect.DeepEqual(created,again){t.Fatal("replay revalidated unavailable model",again)}
	f.failEnsure.Store(0);f.due(t,f.deployment)
	// New worker instance recovers solely from durable DB state, without a POST.
	ctx,cancel:=context.WithCancel(context.Background());done:=make(chan struct{})
	go func(){defer close(done);f.deploy.RunSubmissions(ctx)}()
	defer func(){cancel();<-done}()
	deadline:=time.Now().Add(10*time.Second)
	for count(t,f.deployment,`SELECT count(*) FROM operation_submissions WHERE status='delivered'`)!=1{
		if time.Now().After(deadline){t.Fatal("worker did not recover accepted request")};time.Sleep(20*time.Millisecond)
	}
	if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("recovery event count")}
}

func TestCommittedOperationWithLostResponseRetainsImportCredential(t *testing.T){
	f:=newFixture(t);f.lostReply.Store(true)
	tc:=f.routes()[1]
	code,created:=f.request(t,"POST",tc.path,tc.body,"lost-reply-key");if code!=202{t.Fatal(code,created)}
	id:=created["model_version_id"].(string)
	if count(t,f.management,`SELECT count(*) FROM model_versions WHERE model_version_id=$1 AND state='importing'`,id)!=1 || count(t,f.management,`SELECT count(*) FROM import_credentials WHERE model_version_id=$1`,id)!=1{t.Fatal("lost reply failed domain or removed credential")}
	if count(t,f.ops,`SELECT count(*) FROM operations WHERE operation_id=$1`,created["operation_id"])!=1{t.Fatal("remote commit missing")}
	f.due(t,f.management)
	if _,err:=f.worker(f.management).Deliver(context.Background(),created["operation_id"].(string));err!=nil{t.Fatal(err)}
	if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1 || count(t,f.management,`SELECT count(*) FROM operation_submissions WHERE status='delivered'`)!=1{t.Fatal("lost reply created duplicate")}
}

func TestDomainAndCredentialRollbackWhenSubmissionCannotCommit(t *testing.T){
	f:=newFixture(t)
	execSQL(t,f.management,`ALTER TABLE operation_submissions ADD CONSTRAINT test_reject_submission CHECK(FALSE)`)
	tc:=f.routes()[1];code,_:=f.request(t,"POST",tc.path,tc.body,"rollback-key");if code!=500{t.Fatal("write failure hidden",code)}
	if count(t,f.management,`SELECT count(*) FROM model_versions WHERE version='new'`)!=0 || count(t,f.management,`SELECT count(*) FROM import_credentials`)!=0 || count(t,f.management,`SELECT count(*) FROM api_requests`)!=0{t.Fatal("partial domain/credential/request commit")}
	execSQL(t,f.management,`ALTER TABLE operation_submissions DROP CONSTRAINT test_reject_submission`)
	code,_=f.request(t,"POST",tc.path,tc.body,"rollback-key");if code!=202{t.Fatal("rollback key could not retry",code)}
}

func TestSubmissionAcknowledgementGapAndBlockedDelivery(t *testing.T){
	f:=newFixture(t)
	execSQL(t,f.deployment,`ALTER TABLE operation_submissions ADD CONSTRAINT test_reject_ack CHECK(status<>'delivered')`)
	code,created:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"ack-gap");if code!=202{t.Fatal(code)}
	if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1 || count(t,f.deployment,`SELECT count(*) FROM operation_submissions WHERE status='pending'`)!=1{t.Fatal("ack gap not persisted")}
	execSQL(t,f.deployment,`ALTER TABLE operation_submissions DROP CONSTRAINT test_reject_ack`)
	if _,err:=f.worker(f.deployment).Deliver(context.Background(),created["operation_id"].(string));err!=nil{t.Fatal(err)}
	if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("ack retry duplicated event")}
	f.failEnsure.Store(int32(codes.InvalidArgument))
	code,blocked:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"blocked-key");if code!=202{t.Fatal(code)}
	_,view:=f.request(t,"GET","/api/deployments/"+blocked["deployment_id"].(string),"")
	if view["submission_status"]!="blocked" || view["submission_error_code"]!="InvalidArgument" || view["status"]!="created"{t.Fatal("blocked delivery was hidden or failed live domain",view)}
	if found,err:=f.worker(f.deployment).Deliver(context.Background(),blocked["operation_id"].(string));err!=nil || found{t.Fatal("blocked delivery retried")}
}

func TestOldOperationServerDoesNotCauseLegacyFallback(t *testing.T){
	f:=newFixture(t);f.failEnsure.Store(int32(codes.Unimplemented))
	code,created:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"old-server");if code!=202{t.Fatal(code)}
	if f.legacyCalls.Load()!=0 || count(t,f.ops,`SELECT count(*) FROM operations`)!=0{t.Fatal("old server caused unsafe creation")}
	f.failEnsure.Store(0);f.due(t,f.deployment)
	if _,err:=f.worker(f.deployment).Deliver(context.Background(),created["operation_id"].(string));err!=nil{t.Fatal(err)}
	if count(t,f.ops,`SELECT count(*) FROM operation_outbox`)!=1{t.Fatal("upgrade did not deliver once")}
}

func TestSubmissionSkipsLockedItem(t *testing.T){
	f:=newFixture(t);f.failEnsure.Store(int32(codes.Unavailable))
	_,first:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"locked-1")
	_,second:=f.request(t,"POST","/api/deployments",f.deploymentBody(),"locked-2")
	f.failEnsure.Store(0);f.due(t,f.deployment)
	ctx:=context.Background();tx,err:=f.deployment.Begin(ctx);if err!=nil{t.Fatal(err)};defer tx.Rollback(ctx)
	if _,err=tx.Exec(ctx,`SELECT operation_id FROM operation_submissions WHERE operation_id=$1 FOR UPDATE`,first["operation_id"]);err!=nil{t.Fatal(err)}
	found,err:=f.worker(f.deployment).Deliver(ctx,"");if err!=nil || !found{t.Fatal("locked item blocked queue",err)}
	if count(t,f.ops,`SELECT count(*) FROM operations WHERE operation_id=$1`,second["operation_id"])!=1{t.Fatal("unlocked request not delivered")}
}

func TestImportReplayDoesNotExtendCredentialTTL(t *testing.T){
	f:=newFixture(t);tc:=f.routes()[1]
	code,first:=f.request(t,"POST",tc.path,tc.body,"credential-key");if code!=202{t.Fatal(code)}
	id:=first["model_version_id"].(string)
	execSQL(t,f.management,`UPDATE import_credentials SET expires_at=clock_timestamp()-interval '1 minute' WHERE model_version_id=$1`,id)
	// Startup cleanup removes the row but keeps the credential-required marker.
	if err:=managementapp.InitSchema(context.Background(),f.management);err!=nil{t.Fatal(err)}
	code,again:=f.request(t,"POST",tc.path,tc.body,"credential-key");if code!=202 || !reflect.DeepEqual(first,again){t.Fatal("replay failed",again)}
	if count(t,f.management,`SELECT count(*) FROM import_credentials WHERE model_version_id=$1`,id)!=0{t.Fatal("replay refreshed expired credential")}
	if _,err:=f.clients.Management.GetImportCredential(context.Background(),&managementv1.GetImportCredentialRequest{ModelVersionId:id});status.Code(err)!=codes.FailedPrecondition{t.Fatal("expired credential became anonymous import",err)}
}
