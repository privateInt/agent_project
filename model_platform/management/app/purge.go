package app

import (
	"context"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
)

func (s *Service) SetPurgeServices(d deploymentv1.DeploymentServiceClient, t trainingv1.TrainingServiceClient) {
	s.purgeDeployment = d
	s.purgeTraining = t
}
func (s *Service) PurgeModel(ctx context.Context, r *managementv1.GetModelRequest) (*managementv1.Model, error) {
	if e := purge.ValidID(r.GetModelId()); e != nil {
		return nil, e
	}
	unlock, e := purge.Lock(ctx, s.pool, r.ModelId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer unlock()
	model, e := s.GetModel(ctx, r)
	if e != nil {
		return nil, e
	}
	if model.PurgedAt != nil {
		return model, nil
	}
	if s.purgeDeployment == nil || s.purgeTraining == nil || s.modelFiles == nil {
		return nil, status.Error(codes.Unavailable, "permanent deletion dependencies unavailable")
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer tx.Rollback(context.Background())
	// Import creation and training-output registration take this same model row.
	var modelID string
	if e = tx.QueryRow(ctx, `SELECT model_id::text FROM models WHERE model_id=$1 FOR UPDATE`, r.ModelId).Scan(&modelID); e != nil {
		return nil, idempotency.DBError(e)
	}
	type version struct{ id, state, source, op string }
	var versions []version
	rows, e := tx.Query(ctx, `SELECT v.model_version_id::text,v.state,v.source_type,COALESCE(q.operation_id::text,'') FROM model_versions v LEFT JOIN operation_submissions q ON q.resource_id=v.model_version_id AND q.resource_type='model_version' WHERE v.model_id=$1 ORDER BY v.model_version_id`, r.ModelId)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	for rows.Next() {
		var v version
		if e = rows.Scan(&v.id, &v.state, &v.source, &v.op); e != nil {
			rows.Close()
			return nil, e
		}
		versions = append(versions, v)
	}
	e = rows.Err()
	rows.Close()
	if e != nil {
		return nil, e
	}
	for _, v := range versions {
		if v.state == "importing" {
			return nil, status.Error(codes.FailedPrecondition, "MODEL_IMPORTING: wait for model import or output publication to finish")
		}
		if v.source == "huggingface" {
			reader, ok := s.operations.(interface {
				Get(context.Context, string) (*operationv1.Operation, error)
			})
			if !ok || v.op == "" {
				return nil, status.Error(codes.Unavailable, "model import operation check unavailable")
			}
			op, e := reader.Get(ctx, v.op)
			if e != nil {
				return nil, e
			}
			if op.GetOperationId() != v.op || op.GetResourceId() != v.id || op.GetResourceType() != "model_version" || (op.GetStatus() != "succeeded" && op.GetStatus() != "failed" && op.GetStatus() != "cancelled") {
				return nil, status.Error(codes.FailedPrecondition, "MODEL_IMPORTING: import operation is not terminal")
			}
		}
	}
	for _, v := range versions {
		d, e := s.purgeDeployment.BlockModelPurge(ctx, &deploymentv1.ModelPurgeFence{ModelVersionId: v.id})
		if e != nil {
			return nil, e
		}
		if d.GetModelVersionId() != v.id {
			return nil, status.Error(codes.DataLoss, "deployment purge fence identity mismatch")
		}
		t, e := s.purgeTraining.BlockModelPurge(ctx, &trainingv1.ModelPurgeFence{ModelVersionId: v.id})
		if e != nil {
			return nil, e
		}
		if t.GetModelVersionId() != v.id {
			return nil, status.Error(codes.DataLoss, "training purge fence identity mismatch")
		}
	}
	if _, e = tx.Exec(ctx, `UPDATE models SET purge_state='purging' WHERE model_id=$1`, r.ModelId); e != nil {
		return nil, e
	}
	if _, e = tx.Exec(ctx, `UPDATE model_versions SET state='deleting',updated_at=clock_timestamp() WHERE model_id=$1`, r.ModelId); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	if e = s.modelFiles.PurgeModel(ctx, r.ModelId); e != nil {
		return nil, status.Error(codes.Unavailable, "model file deletion incomplete; retry the same model")
	}
	tx, e = s.pool.Begin(ctx)
	if e != nil {
		return nil, e
	}
	defer tx.Rollback(context.Background())
	if _, e = tx.Exec(ctx, `UPDATE model_versions SET state='deleted',artifact_uri=NULL,base_model_artifact_uri='',updated_at=clock_timestamp() WHERE model_id=$1`, r.ModelId); e != nil {
		return nil, e
	}
	if _, e = tx.Exec(ctx, `DELETE FROM import_credentials WHERE model_version_id IN (SELECT model_version_id FROM model_versions WHERE model_id=$1)`, r.ModelId); e != nil {
		return nil, e
	}
	if _, e = tx.Exec(ctx, `UPDATE models SET purge_state='purged',purged_at=COALESCE(purged_at,clock_timestamp()),deleted_at=COALESCE(deleted_at,clock_timestamp()),updated_at=clock_timestamp() WHERE model_id=$1`, r.ModelId); e != nil {
		return nil, e
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return s.GetModel(ctx, r)
}
