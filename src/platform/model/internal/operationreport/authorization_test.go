package operationreport

import (
    "context"
    "testing"

    "github.com/google/uuid"
    operationv1 "model-platform/gen/operation/v1"
    "google.golang.org/grpc"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/proto"
)

type gateClient struct {
    operationv1.OperationServiceClient
    begins []*operationv1.BeginExecutionRequest
    updates []*operationv1.UpdateOperationRequest
    failures []*operationv1.FailOperationRequest
    failFirst bool
    deny bool
}
func (f *gateClient) BeginExecution(_ context.Context, r *operationv1.BeginExecutionRequest, _ ...grpc.CallOption)(*operationv1.Operation,error){
    f.begins=append(f.begins,proto.Clone(r).(*operationv1.BeginExecutionRequest))
    if f.deny{return nil,status.Error(codes.FailedPrecondition,"stale process")}
    if f.failFirst&&len(f.begins)==1{return nil,status.Error(codes.Unavailable,"reply lost after grant")}
    return &operationv1.Operation{OperationId:r.GetOperationId(),Status:"running",DispatchToken:r.GetDispatchToken(),ExecutionInstanceId:r.GetExecutionInstanceId()},nil
}
func(f *gateClient)MarkRunning(_ context.Context,r *operationv1.UpdateOperationRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){f.updates=append(f.updates,r);return &operationv1.Operation{},nil}
func(f *gateClient)UpdatePhase(_ context.Context,r *operationv1.UpdateOperationRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){f.updates=append(f.updates,r);return &operationv1.Operation{},nil}
func(f *gateClient)SucceedOperation(_ context.Context,r *operationv1.UpdateOperationRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){f.updates=append(f.updates,r);return &operationv1.Operation{},nil}
func(f *gateClient)FailOperation(_ context.Context,r *operationv1.FailOperationRequest,_ ...grpc.CallOption)(*operationv1.Operation,error){f.failures=append(f.failures,r);return &operationv1.Operation{},nil}

func TestAuthorizationRetryKeepsProcessIdentity(t *testing.T){
    token:=uuid.NewString();t.Setenv("DISPATCH_TOKEN",token)
    f:=&gateClient{failFirst:true};c:=New(f);ctx:=context.Background();id:=uuid.NewString();resource:=uuid.NewString()
    if err:=c.Authorize(ctx,id,resource,"training.run");err!=nil{t.Fatal(err)}
    if len(f.begins)!=2||!proto.Equal(f.begins[0],f.begins[1]){t.Fatal("transport retry changed process identity")}
    first:=f.begins[0]
    if first.GetResourceId()!=resource||first.GetExecutionType()!="training.run"||first.GetDispatchToken()!=token{t.Fatal("gate identity missing")}
    if _,err:=uuid.Parse(first.GetExecutionInstanceId());err!=nil{t.Fatal(err)}
    update:=&operationv1.UpdateOperationRequest{OperationId:id,Phase:"test"}
    fail:=&operationv1.FailOperationRequest{OperationId:id,ErrorCode:"TEST"}
    if _,err:=c.MarkRunning(ctx,update);err!=nil{t.Fatal(err)}
    if _,err:=c.UpdatePhase(ctx,update);err!=nil{t.Fatal(err)}
    if _,err:=c.SucceedOperation(ctx,update);err!=nil{t.Fatal(err)}
    if _,err:=c.FailOperation(ctx,fail);err!=nil{t.Fatal(err)}
    if len(f.updates)!=3||len(f.failures)!=1{t.Fatal("report missing")}
    for _,r:=range f.updates{if r.GetDispatchToken()!=token||r.GetExecutionInstanceId()!=first.GetExecutionInstanceId(){t.Fatal("unfenced report")}}
    if f.failures[0].GetDispatchToken()!=token||f.failures[0].GetExecutionInstanceId()!=first.GetExecutionInstanceId(){t.Fatal("unfenced failure")}
    if update.GetDispatchToken()!=""||fail.GetDispatchToken()!=""{t.Fatal("mutated caller request")}
    // A new executable/client gets a distinct process ID even for the same
    // dispatch token, so the server can reject a Docker restart.
    if err:=New(f).Authorize(ctx,id,resource,"training.run");err!=nil{t.Fatal(err)}
    if f.begins[2].GetExecutionInstanceId()==first.GetExecutionInstanceId(){t.Fatal("new process reused execution grant")}
}

func TestAuthorizationDenialDoesNotReportFailure(t *testing.T){
    t.Setenv("DISPATCH_TOKEN",uuid.NewString());f:=&gateClient{deny:true};c:=New(f)
    err:=c.Authorize(context.Background(),uuid.NewString(),uuid.NewString(),"training.run")
    if status.Code(err)!=codes.FailedPrecondition||len(f.begins)!=1||len(f.failures)!=0||len(f.updates)!=0||c.token!=""||c.instance!=""{t.Fatal("denied process retained ownership or reported")}
}
func TestAuthorizationRequiresDispatchToken(t *testing.T){
    t.Setenv("DISPATCH_TOKEN","");f:=&gateClient{};c:=New(f)
    if err:=c.Authorize(context.Background(),uuid.NewString(),uuid.NewString(),"training.run");err==nil||len(f.begins)!=0{t.Fatal("missing token reached execution gate")}
}
