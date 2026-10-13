package app

import (
 "context"
 "encoding/json"
 "errors"
 "log"
 "os"
 "strings"
 "time"
 operationv1 "model-platform/gen/operation/v1"
 trainingv1 "model-platform/gen/training/v1"
 "model-platform/internal/execution"
 "model-platform/internal/gpuadmission"
 "github.com/google/uuid"
 "github.com/jackc/pgx/v5"
 "google.golang.org/grpc/codes"
 "google.golang.org/grpc/status"
 "google.golang.org/protobuf/proto"
 "google.golang.org/protobuf/types/known/timestamppb"
)

type JobControl interface{
 Cancel(context.Context,*operationv1.Operation)error
 Logs(context.Context,*operationv1.Operation,int,int)(*trainingv1.TrainingLogs,error)
}
func(s *Service)ConfigureJobs(control JobControl){s.jobControl=control}
func(s *Service)ConfigureTracking(sync func(context.Context,string)error){s.trackingSync=sync}
func optionalTimestamp(t *time.Time)*timestamppb.Timestamp{if t==nil{return nil};return timestamppb.New(*t)}
func localTraining(node string)bool{return node==""||node==strings.TrimSpace(os.Getenv("DISPATCHER_NODE_NAME"))}
func trainingID(id string)(string,error){
 value,err:=uuid.Parse(strings.TrimSpace(id));if err!=nil||value==uuid.Nil{return "",status.Error(codes.InvalidArgument,"valid training_run_id required")};return value.String(),nil
}

// This row lock arbitrates cancellation against CompleteTraining/FailTraining.
// Replays retain the first request time; completed output is never overwritten.
func(s *Service)CancelTraining(ctx context.Context,r *trainingv1.GetTrainingRunRequest)(*trainingv1.TrainingRun,error){
 id,err:=trainingID(r.GetTrainingRunId());if err!=nil{return nil,err}
 tx,err:=s.pool.Begin(ctx);if err!=nil{return nil,status.Error(codes.Unavailable,"begin training cancellation")};defer tx.Rollback(ctx)
 var state string;var resourcesJSON []byte
 err=tx.QueryRow(ctx,`SELECT status,resources FROM training_runs WHERE training_run_id=$1 FOR UPDATE`,id).Scan(&state,&resourcesJSON)
 if errors.Is(err,pgx.ErrNoRows){return nil,status.Error(codes.NotFound,"training run not found")};if err!=nil{return nil,status.Error(codes.Internal,"load training cancellation")}
 if state!="succeeded"&&state!="failed"&&state!="cancelled"&&state!="cancelling"{
  var resources execution.Resources
  if json.Unmarshal(resourcesJSON,&resources)!=nil{return nil,status.Error(codes.Internal,"stored resources invalid")}
  if !localTraining(resources.NodeName){return nil,status.Error(codes.FailedPrecondition,"remote training cancellation is not supported")}
  if s.jobControl==nil{return nil,status.Error(codes.Unavailable,"training cancellation controller unavailable")}
  var exists bool
  if err=tx.QueryRow(ctx,`SELECT EXISTS(SELECT 1 FROM operation_submissions WHERE resource_type='training_run' AND resource_id=$1)`,id).Scan(&exists);err!=nil{return nil,status.Error(codes.Internal,"lookup training execution")}
  if !exists{return nil,status.Error(codes.FailedPrecondition,"legacy training has no reserved operation identity")}
  _,err=tx.Exec(ctx,`UPDATE training_runs SET status='cancelling',cancel_requested_at=clock_timestamp(),cancellation_error_code='',cancellation_checked_at=NULL,updated_at=clock_timestamp() WHERE training_run_id=$1`,id)
  if err!=nil{return nil,status.Error(codes.Internal,"save training cancellation")}
 }
 if err=tx.Commit(ctx);err!=nil{return nil,status.Error(codes.Unavailable,"commit training cancellation")}
 return s.get(ctx,id)
}

// Separate durable stages permit retries after RPC reply loss, Docker timeout,
// API timeout or process restart. The host guard owns cross-service creation
// exclusion; the DB row serializes multiple cancellation workers.
func(s *Service)ProcessCancellation(ctx context.Context,id string)error{
 tx,err:=s.pool.Begin(ctx);if err!=nil{return err};defer tx.Rollback(ctx)
 var runID,runStatus,trackingID string
 err=tx.QueryRow(ctx,`SELECT training_run_id,status,mlflow_run_id FROM training_runs WHERE (status='cancelling' OR (status='cancelled' AND cancellation_error_code='CANCEL_TRACKING_SYNC_PENDING')) AND ($1::text='' OR training_run_id::text=$1)
 AND (cancellation_checked_at IS NULL OR cancellation_checked_at<clock_timestamp()-INTERVAL '5 seconds')
 ORDER BY cancellation_checked_at NULLS FIRST,training_run_id LIMIT 1 FOR UPDATE SKIP LOCKED`,id).Scan(&runID,&runStatus,&trackingID)
 if errors.Is(err,pgx.ErrNoRows){return nil};if err!=nil{return err}
 var requestBytes []byte
 err=tx.QueryRow(ctx,`SELECT request FROM operation_submissions WHERE resource_type='training_run' AND resource_id=$1`,runID).Scan(&requestBytes)
 var create operationv1.CreateOperationRequest
 code:="";finalState:=""
 if runStatus=="cancelled" {finalState="cancelled"}else if err!=nil||proto.Unmarshal(requestBytes,&create)!=nil||create.GetResourceId()!=runID||create.GetExecutionType()!="training.run"{code="CANCEL_EXECUTION_IDENTITY_INVALID"}else{
  // Ensure uses the original durable request. It cannot create a different Job
  // when the first submission reply was lost or its acknowledgement is delayed.
  op,e:=s.clients.Operation.EnsureOperation(ctx,&create)
  if e!=nil{code="CANCEL_OPERATION_UNAVAILABLE"}else if !matchesTraining(op,runID,create.GetOperationId()){code="CANCEL_EXECUTION_IDENTITY_INVALID"}else{
   op,e=s.clients.Operation.RequestTrainingCancellation(ctx,&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:runID})
   if e!=nil{code="CANCEL_OPERATION_UNAVAILABLE"}else if !matchesTraining(op,runID,create.GetOperationId()){code="CANCEL_EXECUTION_IDENTITY_INVALID"}else{
    switch op.GetStatus(){
    case "cancelled":finalState="cancelled"
    case "succeeded":code="CANCEL_OPERATION_ALREADY_SUCCEEDED" // requires domain evidence; never invent a result
    case "failed","cancelling":
     stopped:=proto.Clone(op).(*operationv1.Operation);stopped.Status="cancelling"
     if s.jobControl==nil{code="CANCEL_CONTROLLER_UNAVAILABLE"}else if e=s.jobControl.Cancel(ctx,stopped);e!=nil{
      code="CANCEL_STOP_UNCONFIRMED";var unconfirmed *gpuadmission.CreationUnconfirmedError;if errors.As(e,&unconfirmed){code="CANCEL_CREATION_UNCONFIRMED"}
     }else if op.GetStatus()=="failed"{
      // A legacy Job's failure report may have won before the Operation fence.
      // Preserve that terminal outcome after confirming that the Job stopped.
      finalState="failed";code="CANCEL_LOST_TO_FAILURE"
     }else{
      done,e:=s.clients.Operation.ConfirmTrainingCancellation(ctx,&operationv1.TrainingCancellationRequest{OperationId:op.GetOperationId(),TrainingRunId:runID,ExpectedDispatchToken:op.GetDispatchToken()})
      if e!=nil||!matchesTraining(done,runID,create.GetOperationId())||done.GetStatus()!="cancelled"{code="CANCEL_CONFIRMATION_UNAVAILABLE"}else{finalState="cancelled"}
     }
    default:code="CANCEL_OPERATION_STATE_INVALID"
    }
   }
  }
 }
 if finalState=="cancelled"&&trackingID!=""{
  if s.trackingSync==nil{code="CANCEL_TRACKING_SYNC_PENDING"}else{
   trackingCtx,cancel:=context.WithTimeout(ctx,10*time.Second);trackingErr:=s.trackingSync(trackingCtx,trackingID);cancel()
   if trackingErr!=nil{code="CANCEL_TRACKING_SYNC_PENDING"}
  }
 }
 // Rollback on DB failure is safe: Operation/host cancellation are monotonic.
 _,err=tx.Exec(ctx,`UPDATE training_runs SET status=CASE WHEN $2::text='' THEN status ELSE $2::varchar END,
 cancelled_at=CASE WHEN $2::text='cancelled' THEN COALESCE(cancelled_at,clock_timestamp()) ELSE cancelled_at END,
 cancellation_error_code=$3,cancellation_checked_at=clock_timestamp(),updated_at=clock_timestamp() WHERE training_run_id=$1`,runID,finalState,code)
 if err!=nil{return err};if err=tx.Commit(ctx);err!=nil{return err}
 if code!=""{log.Printf("training cancellation run=%s code=%s",runID,code)}
 return nil
}
func matchesTraining(op *operationv1.Operation,runID,opID string)bool{return op!=nil&&op.GetOperationId()==opID&&op.GetResourceId()==runID&&op.GetResourceType()=="training_run"&&op.GetExecutionType()=="training.run"&&op.GetDispatchProtocol()==1}
func(s *Service)RunCancellations(ctx context.Context){
 log.Print("training cancellation controller started (local Docker)")
 for ctx.Err()==nil{
  c,cancel:=context.WithTimeout(ctx,45*time.Second);err:=s.ProcessCancellation(c,"");cancel()
  if err!=nil{log.Print("training cancellation cycle unavailable")}
  select{case <-ctx.Done():return;case <-time.After(time.Second):}
 }
}
func(s *Service)GetTrainingLogs(ctx context.Context,r *trainingv1.GetTrainingLogsRequest)(*trainingv1.TrainingLogs,error){
 id,err:=trainingID(r.GetTrainingRunId());if err!=nil{return nil,err}
 lines,max:=r.GetTailLines(),r.GetMaxBytes();if lines==0{lines=200};if max==0{max=65536}
 if lines<1||lines>2000||max<1024||max>262144{return nil,status.Error(codes.InvalidArgument,"tail_lines must be 1..2000 and max_bytes 1024..262144")}
 run,err:=s.get(ctx,id);if err!=nil{return nil,err}
 if !localTraining(run.GetResources().GetNodeName()){return nil,status.Error(codes.FailedPrecondition,"remote training logs are not supported")}
 none:=&trainingv1.TrainingLogs{TrainingRunId:id,OperationId:run.GetOperationId(),Source:"none",Availability:"not_started",TailLines:lines,MaxBytes:max}
 if run.GetOperationId()==""{none.Availability="legacy_execution_unavailable";return none,nil}
 op,err:=s.clients.Operation.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:run.GetOperationId()})
 if status.Code(err)==codes.NotFound&&run.GetSubmissionStatus()!="delivered"{return none,nil}
 if err!=nil{return nil,status.Error(codes.Unavailable,"training operation lookup unavailable")}
 if !matchesTraining(op,id,run.GetOperationId()){return nil,status.Error(codes.FailedPrecondition,"training operation identity mismatch")}
 if s.jobControl==nil{return nil,status.Error(codes.Unavailable,"training log reader unavailable")}
 logs,err:=s.jobControl.Logs(ctx,op,int(lines),int(max));if err!=nil{return nil,status.Error(codes.Unavailable,"training logs unavailable; retry or inspect the retained Job/archive")}
 if logs.GetTrainingRunId()!=id||logs.GetOperationId()!=op.GetOperationId(){return nil,status.Error(codes.Internal,"training log identity mismatch")}
 return logs,nil
}
