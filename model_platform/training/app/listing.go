package app

import (
    "context"
    "encoding/json"
    "time"
    trainingv1 "model-platform/gen/training/v1"
    "model-platform/internal/execution"
    "model-platform/internal/trainingcheckpoint"
    "model-platform/internal/idempotency"
    "model-platform/internal/listing"
    "google.golang.org/protobuf/types/known/timestamppb"
)

func (s *Service) ListTrainingRuns(ctx context.Context, req *trainingv1.ListTrainingRunsRequest) (*trainingv1.ListTrainingRunsResponse,error) {
    state,err:=listing.Enum(req.GetStatus(),"status","created","running","cancelling","cancelled","succeeded","failed");if err!=nil{return nil,err}
    source,err:=listing.UUID(req.GetSourceModelVersionId(),"source_model_version_id");if err!=nil{return nil,err}
    dataset,err:=listing.UUID(req.GetDatasetVersionId(),"dataset_version_id");if err!=nil{return nil,err}
    page,err:=listing.New(req.GetLimit(),req.GetCursor(),"training-runs",state,source,dataset);if err!=nil{return nil,err}
    query:=listing.Query{}
    query.Equal("r.status",state);query.Equal("r.source_model_version_id",source);query.Equal("r.dataset_spec->>'dataset_version_id'",dataset)
    suffix:=query.Finish(page,"r.created_at","r.training_run_id")
    // No per-item RPC/SELECT and no large result_json in a selection list.
    rows,err:=s.pool.Query(ctx,`SELECT r.training_run_id,r.source_model_version_id,r.status,r.resources,r.resume_spec,r.evaluation_spec,r.dataset_spec,r.training_spec,
        r.mlflow_experiment_id,r.mlflow_run_id,r.mlflow_run_url,COALESCE(r.output_model_version_id::text,''),r.created_at,r.updated_at,
        COALESCE(s.operation_id::text,''),COALESCE(s.status,''),COALESCE(s.last_error_code,''),r.cancel_requested_at,r.cancelled_at,r.cancellation_error_code,COALESCE(o.status,''),COALESCE(o.output_model_version_id,'')
        FROM training_runs r LEFT JOIN operation_submissions s ON s.resource_type='training_run' AND s.resource_id=r.training_run_id LEFT JOIN LATERAL (SELECT status,output_model_version_id FROM training_output_recoveries WHERE training_run_id=r.training_run_id ORDER BY requested_at DESC,recovery_id DESC LIMIT 1) o ON true`+suffix,query.Args...)
    if err!=nil{return nil,idempotency.DBError(err)};defer rows.Close()
    out:=&trainingv1.ListTrainingRunsResponse{}
    for rows.Next(){
        if len(out.Runs)==page.Limit {last:=out.Runs[len(out.Runs)-1];out.NextCursor=page.Next(last.CreatedAt.AsTime(),last.TrainingRunId);break}
        r:=&trainingv1.TrainingRun{};var created,updated time.Time;var requested,cancelled *time.Time;var resourceJSON,datasetJSON,trainingJSON,resumeJSON,evaluationJSON []byte
        if err:=rows.Scan(&r.TrainingRunId,&r.SourceModelVersionId,&r.Status,&resourceJSON,&resumeJSON,&evaluationJSON,&datasetJSON,&trainingJSON,
            &r.MlflowExperimentId,&r.MlflowRunId,&r.MlflowRunUrl,&r.OutputModelVersionId,&created,&updated,
            &r.OperationId,&r.SubmissionStatus,&r.SubmissionErrorCode,&requested,&cancelled,&r.CancellationErrorCode,&r.OutputRecoveryStatus,&r.RecoveredModelVersionId);err!=nil{return nil,idempotency.DBError(err)}
        r.CancelRequestedAt=optionalTimestamp(requested);r.CancelledAt=optionalTimestamp(cancelled)
        r.Evaluation,err=storedEvaluation(evaluationJSON);if err!=nil{return nil,err}
        var resume trainingcheckpoint.Resume
        if err:=json.Unmarshal(resumeJSON,&resume);err!=nil{return nil,idempotency.DBError(err)}
        r.Resume=resume.Proto()
        var resources execution.Resources;var spec TrainingSpec;var data DatasetSpec
        for _,value:=range []struct{raw []byte;target any}{{resourceJSON,&resources},{datasetJSON,&data},{trainingJSON,&spec}} {
            if err:=json.Unmarshal(value.raw,value.target);err!=nil{return nil,idempotency.DBError(err)}
        }
        if data.SourceType=="" {data,_=NormalizeDatasetSpec(nil)}
        if spec.Method=="" {spec,_=NormalizeTrainingSpec(nil)}
        r.Resources=resources.Proto();r.Dataset=data.Proto();r.Training=spec.Proto()
        r.CreatedAt=timestamppb.New(created);r.UpdatedAt=timestamppb.New(updated)
        out.Runs=append(out.Runs,r)
    }
    return out,idempotency.DBError(rows.Err())
}
