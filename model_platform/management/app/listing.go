package app

import (
	"context"
	"google.golang.org/protobuf/types/known/timestamppb"
	managementv1 "model-platform/gen/management/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/listing"
	"time"
)

func (s *Service) ListModels(ctx context.Context, req *managementv1.ListModelsRequest) (*managementv1.ListModelsResponse, error) {
	text, err := listing.Text(req.GetQ())
	if err != nil {
		return nil, err
	}
	kind, err := listing.Enum(req.GetModelType(), "model_type", "llm", "embedding", "reranker", "ocr")
	if err != nil {
		return nil, err
	}
	filters := []string{text, kind}
	if req.GetIncludeDeleted() {
		filters = append(filters, "include-deleted")
	}
	page, err := listing.New(req.GetLimit(), req.GetCursor(), "models", filters...)
	if err != nil {
		return nil, err
	}
	query := listing.Query{}
	query.Equal("model_type", kind)
	query.Contains(text, "name", "description")
	suffix := query.Finish(page, "created_at", "model_id")
	visible := "deleted_at IS NULL AND purged_at IS NULL"
	if req.GetIncludeDeleted() {
		visible = "purged_at IS NULL"
	}
	rows, err := s.pool.Query(ctx, `SELECT model_id,name,model_type,description,created_at,updated_at,deleted_at,purge_state,purged_at FROM (SELECT * FROM models WHERE `+visible+`) models`+suffix, query.Args...)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	defer rows.Close()
	out := &managementv1.ListModelsResponse{}
	for rows.Next() {
		if len(out.Models) == page.Limit {
			last := out.Models[len(out.Models)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.ModelId)
			break
		}
		model := &managementv1.Model{}
		var created, updated time.Time
		var deleted, purged *time.Time
		if err := rows.Scan(&model.ModelId, &model.Name, &model.ModelType, &model.Description, &created, &updated, &deleted, &model.PurgeState, &purged); err != nil {
			return nil, idempotency.DBError(err)
		}
		if deleted != nil {
			model.DeletedAt = timestamppb.New(*deleted)
		}
		if purged != nil {
			model.PurgedAt = timestamppb.New(*purged)
		}
		model.CreatedAt = timestamppb.New(created)
		model.UpdatedAt = timestamppb.New(updated)
		out.Models = append(out.Models, model)
	}
	return out, idempotency.DBError(rows.Err())
}

func (s *Service) ListModelVersions(ctx context.Context, req *managementv1.ListModelVersionsRequest) (*managementv1.ListModelVersionsResponse, error) {
	text, err := listing.Text(req.GetQ())
	if err != nil {
		return nil, err
	}
	modelID, err := listing.UUID(req.GetModelId(), "model_id")
	if err != nil {
		return nil, err
	}
	state, err := listing.Enum(req.GetState(), "state", "importing", "ready", "failed", "deleting", "deleted")
	if err != nil {
		return nil, err
	}
	kind, err := listing.Enum(req.GetArtifactKind(), "artifact_kind", "file", "snapshot", "adapter")
	if err != nil {
		return nil, err
	}
	source, err := listing.Enum(req.GetSourceType(), "source_type", "huggingface", "upload", "training")
	if err != nil {
		return nil, err
	}
	page, err := listing.New(req.GetLimit(), req.GetCursor(), "model-versions", text, modelID, state, kind, source)
	if err != nil {
		return nil, err
	}
	query := listing.Query{}
	query.Equal("v.model_id", modelID)
	query.Equal("v.state", state)
	query.Equal("v.artifact_kind", kind)
	query.Equal("v.source_type", source)
	query.Contains(text, "m.name", "v.version", "v.repo_id")
	suffix := query.Finish(page, "v.created_at", "v.model_version_id")
	rows, err := s.pool.Query(ctx, `SELECT v.model_version_id,v.model_id,m.name,m.model_type,v.version,v.state,v.source_type,v.artifact_kind,v.repo_id,
        COALESCE(v.training_run_id::text,''),COALESCE(v.source_model_version_id::text,''),v.created_at,v.updated_at,
        COALESCE(s.operation_id::text,''),COALESCE(s.status,''),COALESCE(s.last_error_code,'')
        FROM model_versions v JOIN models m ON m.model_id=v.model_id AND m.deleted_at IS NULL
        LEFT JOIN operation_submissions s ON s.resource_type='model_version' AND s.resource_id=v.model_version_id`+suffix, query.Args...)
	if err != nil {
		return nil, idempotency.DBError(err)
	}
	defer rows.Close()
	out := &managementv1.ListModelVersionsResponse{}
	for rows.Next() {
		if len(out.Versions) == page.Limit {
			last := out.Versions[len(out.Versions)-1]
			out.NextCursor = page.Next(last.CreatedAt.AsTime(), last.ModelVersionId)
			break
		}
		v := &managementv1.ModelVersion{}
		var created, updated time.Time
		if err := rows.Scan(&v.ModelVersionId, &v.ModelId, &v.ModelName, &v.ModelType, &v.Version, &v.State, &v.SourceType, &v.ArtifactKind, &v.RepoId,
			&v.TrainingRunId, &v.SourceModelVersionId, &created, &updated, &v.OperationId, &v.SubmissionStatus, &v.SubmissionErrorCode); err != nil {
			return nil, idempotency.DBError(err)
		}
		v.CreatedAt = timestamppb.New(created)
		v.UpdatedAt = timestamppb.New(updated)
		out.Versions = append(out.Versions, v)
	}
	return out, idempotency.DBError(rows.Err())
}
