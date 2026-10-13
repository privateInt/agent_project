package app

import (
	"context"
	"errors"
	"strings"
	"time"

	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/idempotency"
	"model-platform/internal/submission"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type operationCreator interface {
	Ensure(context.Context, *operationv1.CreateOperationRequest) (*operationv1.Operation, error)
}

func (s *Service) submissions() submission.Worker {
	return submission.Worker{Pool: s.pool, Ensure: s.operations.Ensure}
}
func (s *Service) RunSubmissions(ctx context.Context) { s.submissions().Run(ctx) }

func (s *Service) CreateModel(ctx context.Context, req *managementv1.CreateModelRequest) (*managementv1.Model, error) {
	name := strings.TrimSpace(req.GetName())
	modelType := strings.ToLower(strings.TrimSpace(req.GetModelType()))
	description := strings.TrimSpace(req.GetDescription())
	if name == "" {
		return nil, status.Error(codes.InvalidArgument, "name is required")
	}
	if modelType == "" {
		modelType = "llm"
	}
	switch modelType {
	case "llm", "embedding", "reranker", "ocr":
	default:
		return nil, status.Error(codes.InvalidArgument, "model_type must be one of: llm, embedding, reranker, ocr")
	}
	fingerprint, err := idempotency.Fingerprint(struct {
		Version                      int
		Name, ModelType, Description string
	}{1, name, modelType, description}, nil)
	if err != nil {
		return nil, err
	}
	response := &managementv1.Model{}
	err = idempotency.Execute(ctx, s.pool, "model.create.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		id := uuid.NewString()
		var createdAt, updatedAt time.Time
		if err := tx.QueryRow(ctx, `INSERT INTO models(model_id,name,model_type,description) VALUES($1,$2,$3,$4) RETURNING created_at,updated_at`, id, name, modelType, description).Scan(&createdAt, &updatedAt); err != nil {
			return err
		}
		response.ModelId, response.Name, response.ModelType, response.Description = id, name, modelType, description
		response.CreatedAt, response.UpdatedAt = timestamppb.New(createdAt), timestamppb.New(updatedAt)
		return nil
	})
	if err != nil {
		return nil, err
	}
	return response, nil
}

// HF token stays exclusively in the short-lived encrypted credential table.
// HMAC distinguishes changed credentials without persisting the request body.
// Replays do not recreate credentials or extend the original TTL.
func (s *Service) ImportHuggingFace(ctx context.Context, req *managementv1.ImportHuggingFaceRequest) (*managementv1.ImportHuggingFaceResponse, error) {
	parsedID, err := uuid.Parse(strings.TrimSpace(req.GetModelId()))
	if err != nil || parsedID == uuid.Nil {
		return nil, status.Error(codes.InvalidArgument, "model_id must be UUID")
	}
	modelID := parsedID.String()
	version, repoID, revision, token := strings.TrimSpace(req.GetVersion()), strings.TrimSpace(req.GetRepoId()), strings.TrimSpace(req.GetRevision()), strings.TrimSpace(req.GetHfToken())
	if version == "" || repoID == "" {
		return nil, status.Error(codes.InvalidArgument, "version and repo_id are required")
	}
	if revision == "" {
		revision = "main"
	}
	fingerprint, err := idempotency.Fingerprint(struct {
		Version                                       int
		ModelID, VersionName, RepoID, Revision, Token string
	}{1, modelID, version, repoID, revision, token}, s.credentialKey)
	if err != nil {
		return nil, err
	}
	response := &managementv1.ImportHuggingFaceResponse{}
	err = idempotency.Execute(ctx, s.pool, "model.import.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		// Serialize new imports against catalog deletion. Replays are handled
		// by idempotency.Execute before this callback and retain their result.
		var deleted bool
		err := tx.QueryRow(ctx, `SELECT deleted_at IS NOT NULL OR purge_state<>'' FROM models WHERE model_id=$1 FOR UPDATE`, modelID).Scan(&deleted)
		if errors.Is(err, pgx.ErrNoRows) {
			return status.Error(codes.NotFound, "model not found")
		}
		if err != nil {
			return err
		}
		if deleted {
			return status.Error(codes.FailedPrecondition, "model was removed from catalog")
		}
		var nonce, ciphertext []byte
		if token != "" {
			var err error
			nonce, ciphertext, err = encryptCredential(s.credentialKey, token)
			if err != nil {
				return status.Error(codes.Internal, "encrypt import credential")
			}
		}
		id, operationID := uuid.NewString(), uuid.NewString()
		if _, err := tx.Exec(ctx, `INSERT INTO model_versions(model_version_id,model_id,version,state,source_type,source_ref,artifact_kind,repo_id,requested_revision,import_credential_required)
VALUES($1,$2,$3,'importing','huggingface',jsonb_build_object('repo_id',$4::text,'requested_revision',$5::text),'snapshot',$4,$5,$6)`, id, modelID, version, repoID, revision, token != ""); err != nil {
			return err
		}
		if token != "" {
			if _, err := tx.Exec(ctx, `INSERT INTO import_credentials(model_version_id,credential_type,nonce,ciphertext,expires_at) VALUES($1,'huggingface_token',$2,$3,$4)`, id, nonce, ciphertext, time.Now().UTC().Add(importCredentialTTL)); err != nil {
				return err
			}
		}
		if err := submission.Enqueue(ctx, tx, &operationv1.CreateOperationRequest{OperationId: operationID, Kind: "model.import.huggingface", ExecutionType: "management.import-huggingface", ResourceType: "model_version", ResourceId: id, MaxAttempts: 3}); err != nil {
			return err
		}
		response.ModelVersionId, response.OperationId, response.Status = id, operationID, "pending"
		return nil
	})
	if err != nil {
		return nil, err
	}
	s.submissions().Try(ctx, response.GetOperationId())
	return response, nil
}
