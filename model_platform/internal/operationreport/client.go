// Package operationreport retries only idempotent lifecycle reports, never jobs.
package operationreport

import (
 "context"
 "log"
 "time"
 "os"
 "fmt"
 "github.com/google/uuid"
 "google.golang.org/protobuf/proto"
 operationv1 "model-platform/gen/operation/v1"
 "google.golang.org/grpc"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
)

type Client struct { operationv1.OperationServiceClient; token,instance string }
func New(client operationv1.OperationServiceClient) *Client { return &Client{OperationServiceClient:client} }

func report(ctx context.Context, method, id string, call func(context.Context) (*operationv1.Operation,error)) (*operationv1.Operation,error) {
 var result *operationv1.Operation
 var err error
 for attempt:=1; attempt<=3; attempt++ {
  callCtx,cancel := context.WithTimeout(ctx, 5*time.Second)
  result,err = call(callCtx)
  cancel()
  if err == nil { return result,nil }
  log.Printf("operation report failed method=%s operation=%s attempt=%d code=%s error=%v",method,id,attempt,status.Code(err),err)
  if ctx.Err()!=nil || !retryable(err) || attempt==3 { break }
  timer := time.NewTimer(time.Duration(attempt)*time.Second)
  select {
  case <-ctx.Done(): timer.Stop(); return nil,ctx.Err()
  case <-timer.C:
  }
 }
 return nil,err
}
func retryable(err error) bool {
 switch status.Code(err) {
 case codes.Unavailable,codes.DeadlineExceeded: return true
 default: return false
 }
}
func (c *Client) MarkRunning(ctx context.Context,r *operationv1.UpdateOperationRequest,o ...grpc.CallOption)(*operationv1.Operation,error) {
 r=proto.Clone(r).(*operationv1.UpdateOperationRequest);r.DispatchToken=c.token;r.ExecutionInstanceId=c.instance
 return report(ctx,"MarkRunning",r.GetOperationId(),func(ctx context.Context)(*operationv1.Operation,error){return c.OperationServiceClient.MarkRunning(ctx,r,o...)})
}
func (c *Client) SucceedOperation(ctx context.Context,r *operationv1.UpdateOperationRequest,o ...grpc.CallOption)(*operationv1.Operation,error) {
 r=proto.Clone(r).(*operationv1.UpdateOperationRequest);r.DispatchToken=c.token;r.ExecutionInstanceId=c.instance
 return report(ctx,"SucceedOperation",r.GetOperationId(),func(ctx context.Context)(*operationv1.Operation,error){return c.OperationServiceClient.SucceedOperation(ctx,r,o...)})
}
func (c *Client) FailOperation(ctx context.Context,r *operationv1.FailOperationRequest,o ...grpc.CallOption)(*operationv1.Operation,error) {
 r=proto.Clone(r).(*operationv1.FailOperationRequest);r.DispatchToken=c.token;r.ExecutionInstanceId=c.instance
 return report(ctx,"FailOperation",r.GetOperationId(),func(ctx context.Context)(*operationv1.Operation,error){return c.OperationServiceClient.FailOperation(ctx,r,o...)})
}

// Authorize is called before ANY domain reads/writes or compute. A denial or
// lost response after the retry budget stops this process without failure reports.
func(c *Client)Authorize(ctx context.Context,id,resourceID,executionType string)error{
 token:=os.Getenv("DISPATCH_TOKEN")
 if _,err:=uuid.Parse(token);err!=nil{return fmt.Errorf("valid DISPATCH_TOKEN is required")}
 instance:=uuid.NewString()
 req:=&operationv1.BeginExecutionRequest{OperationId:id,DispatchToken:token,ExecutionInstanceId:instance,ResourceId:resourceID,ExecutionType:executionType}
 result,err:=report(ctx,"BeginExecution",id,func(ctx context.Context)(*operationv1.Operation,error){return c.OperationServiceClient.BeginExecution(ctx,req)})
 if err!=nil{return err}
 if result.GetStatus()!="running"||result.GetExecutionInstanceId()!=instance||result.GetDispatchToken()!=token{return fmt.Errorf("execution authorization response mismatch")}
 c.token=token;c.instance=instance
 return nil
}
func(c *Client)UpdatePhase(ctx context.Context,r *operationv1.UpdateOperationRequest,o ...grpc.CallOption)(*operationv1.Operation,error){
 r=proto.Clone(r).(*operationv1.UpdateOperationRequest);r.DispatchToken=c.token;r.ExecutionInstanceId=c.instance
 return c.OperationServiceClient.UpdatePhase(ctx,r,o...)
}

// ExecutionIdentity is available only after Authorize confirmed the grant.
func (c *Client) ExecutionIdentity() (string, string) { return c.token, c.instance }
