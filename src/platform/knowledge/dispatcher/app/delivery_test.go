package app

import (
 "context"
 "errors"
 "testing"
 operationv1 "knowledge-platform/gen/operation/v1"
 "knowledge-platform/internal/execution"
)
const testID="9fc02368-3291-4589-ad66-0e855da0eb85"
var testPayload=[]byte(`{"operation_id":"`+testID+`","execution_type":"untrusted","resource_id":"untrusted"}`)
type fakeOps struct {result *operationv1.ClaimOperationResponse;err,errorFail error;claims,fails int}
func(o *fakeOps)Claim(context.Context,string)(*operationv1.ClaimOperationResponse,error){o.claims++;return o.result,o.err}
func(o *fakeOps)Fail(context.Context,string,string,string,string,string,bool)error{o.fails++;return o.errorFail}
type fakeProfiles struct{err error;kind string}
func(p *fakeProfiles)Get(kind string)(execution.Profile,error){p.kind=kind;return execution.Profile{Image:"test-image"},p.err}
type fakeRunner struct{calls int;cmd execution.Command;err error}
func(r *fakeRunner)Run(_ context.Context,c execution.Command,_ execution.Profile)error{r.calls++;r.cmd=c;return r.err}
func fixture(state string,claimed bool)(Delivery,*fakeOps,*fakeProfiles,*fakeRunner){
 o:=&fakeOps{result:&operationv1.ClaimOperationResponse{Claimed:claimed,Operation:&operationv1.Operation{OperationId:testID,DispatchProtocol:1,DispatchToken:"11111111-1111-4111-8111-111111111111",Status:state,ExecutionType:"chunking.run",ResourceType:"chunking_run",ResourceId:"db-resource"}}}
 p:=&fakeProfiles{};r:=&fakeRunner{}
 return Delivery{Operations:o,Profiles:p,Runner:r},o,p,r
}
func TestClaimFailureRetainsRecord(t *testing.T){d,o,_,r:=fixture("",false);o.err=errors.New("lost claim reply");if d.Handle(context.Background(),testPayload)==nil||r.calls!=0{t.Fatal("uncertain claim was accepted")}}
func TestDispatchingDuplicateRequiresReconciliation(t *testing.T){d,_,_,r:=fixture("dispatching",false);if d.Handle(context.Background(),testPayload)==nil||r.calls!=0{t.Fatal("ambiguous dispatch was accepted or relaunched")}}
func TestKnownDuplicateCanCommit(t *testing.T){for _,s:=range []string{"running","succeeded","failed","cancelling","cancelled"}{d,_,_,r:=fixture(s,false);if err:=d.Handle(context.Background(),testPayload);err!=nil||r.calls!=0{t.Fatalf("state=%s err=%v launches=%d",s,err,r.calls)}}}
func TestRoutingUsesDatabase(t *testing.T){d,_,p,r:=fixture("dispatching",true);if err:=d.Handle(context.Background(),testPayload);err!=nil{t.Fatal(err)};if r.calls!=1||p.kind!="chunking.run"||r.cmd.ResourceID!="db-resource"||r.cmd.DispatchToken!="11111111-1111-4111-8111-111111111111"{t.Fatal("trusted Kafka routing")}}
func TestLaunchErrorDoesNotFailPotentiallyRunningJob(t *testing.T){d,o,_,r:=fixture("dispatching",true);r.err=errors.New("docker timeout");if d.Handle(context.Background(),testPayload)==nil||o.fails!=0{t.Fatal("ambiguous launch was discarded")}}
func TestFailureReportMustBeConfirmed(t *testing.T){d,o,p,r:=fixture("dispatching",true);p.err=errors.New("unknown profile");o.errorFail=errors.New("DB unavailable");if d.Handle(context.Background(),testPayload)==nil||r.calls!=0{t.Fatal("failure report was ignored")}}
func TestInvalidPayloadIsRetained(t *testing.T){d,o,_,_:=fixture("",false);if d.Handle(context.Background(),[]byte("invalid"))==nil||o.claims!=0{t.Fatal("invalid record was silently accepted")}}
func TestCommitRetriesSameAction(t *testing.T){calls:=0;err:=Commit(context.Background(),func(context.Context)error{calls++;if calls<3{return errors.New("transient")};return nil});if err!=nil||calls!=3{t.Fatalf("calls=%d err=%v",calls,err)}}
func TestCommitFailureStops(t *testing.T){calls:=0;err:=Commit(context.Background(),func(context.Context)error{calls++;return errors.New("offline")});if err==nil||calls!=3{t.Fatalf("calls=%d err=%v",calls,err)}}

func TestMissingExecutionGateRetainsRecord(t *testing.T){
 d,o,_,r:=fixture("dispatching",true);o.result.Operation.DispatchToken=""
 if d.Handle(context.Background(),testPayload)==nil||r.calls!=0{t.Fatal("ungated launch accepted")}
}
