package app

import (
	"context"
	"errors"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	deploymentv1 "model-platform/gen/deployment/v1"
	managementv1 "model-platform/gen/management/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/testsupport"
	"testing"
)

type purgeModels struct {
	calls []string
	fail  bool
}

func (m *purgeModels) PurgeModel(_ context.Context, id string) error {
	m.calls = append(m.calls, id)
	if m.fail {
		return errors.New("storage offline")
	}
	return nil
}

type purgeDeployments struct {
	deploymentv1.DeploymentServiceClient
	active bool
	ids    []string
}

func (d *purgeDeployments) BlockModelPurge(_ context.Context, r *deploymentv1.ModelPurgeFence, _ ...grpc.CallOption) (*deploymentv1.ModelPurgeFence, error) {
	d.ids = append(d.ids, r.ModelVersionId)
	if d.active {
		return nil, status.Error(codes.FailedPrecondition, "MODEL_DEPLOYED")
	}
	return r, nil
}

type purgeTrainings struct {
	trainingv1.TrainingServiceClient
	ids []string
}

func (d *purgeTrainings) BlockModelPurge(_ context.Context, r *trainingv1.ModelPurgeFence, _ ...grpc.CallOption) (*trainingv1.ModelPurgeFence, error) {
	d.ids = append(d.ids, r.ModelVersionId)
	return r, nil
}
func TestPurgeModelChecksConsumersBeforeStorageAndResumesFailures(t *testing.T) {
	ctx := context.Background()
	pool := testsupport.Postgres(t)
	if e := InitSchema(ctx, pool); e != nil {
		t.Fatal(e)
	}
	id, version := uuid.NewString(), uuid.NewString()
	d := &purgeDeployments{active: true}
	train := &purgeTrainings{}
	files := &purgeModels{}
	s := &Service{pool: pool, purgeDeployment: d, purgeTraining: train, modelFiles: files}
	if _, e := pool.Exec(ctx, `INSERT INTO models(model_id,name,model_type)VALUES($1,'purge-test','llm');`, id); e != nil {
		t.Fatal(e)
	}
	if _, e := pool.Exec(ctx, `INSERT INTO model_versions(model_version_id,model_id,version,state,source_type) VALUES($1,$2,'v1','ready','training')`, version, id); e != nil {
		t.Fatal(e)
	}
	target := &managementv1.GetModelRequest{ModelId: id}
	if _, e := s.PurgeModel(ctx, target); status.Code(e) != codes.FailedPrecondition || len(files.calls) != 0 {
		t.Fatal("active model files touched", e)
	}
	d.active = false
	files.fail = true
	if _, e := s.PurgeModel(ctx, target); status.Code(e) != codes.Unavailable {
		t.Fatal(e)
	}
	pending, e := s.GetModel(ctx, target)
	if e != nil || pending.PurgeState != "purging" || pending.PurgedAt != nil {
		t.Fatal(pending, e)
	}
	v, e := s.getVersion(ctx, version)
	if e != nil || v.State != "deleting" {
		t.Fatal(v, e)
	}
	files.fail = false
	done, e := s.PurgeModel(ctx, target)
	if e != nil || done.PurgedAt == nil {
		t.Fatal(done, e)
	}
	if _, e = s.PurgeModel(ctx, target); e != nil || len(files.calls) != 2 {
		t.Fatal("purge replay touched storage", e)
	}
	v, e = s.getVersion(ctx, version)
	if e != nil || v.State != "deleted" || v.ArtifactUri != "" {
		t.Fatal(v, e)
	}
	list, e := s.ListModels(ctx, &managementv1.ListModelsRequest{IncludeDeleted: true})
	if e != nil || len(list.Models) != 0 {
		t.Fatal("purged model remains selectable", e)
	}
}
