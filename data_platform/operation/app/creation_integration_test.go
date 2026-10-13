package app

import (
	"context"
	"strings"
	"sync"
	"testing"

	commonv1 "data-platform/gen/common/v1"
	operationv1 "data-platform/gen/operation/v1"
	"data-platform/internal/testsupport"

	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestEnsureOperationCreationIdentity(t *testing.T) {
	pool := testsupport.Postgres(t)
	ctx := context.Background()
	if err := InitSchema(ctx, pool); err != nil {
		t.Fatal(err)
	}
	s := New(pool)
	req := &operationv1.CreateOperationRequest{OperationId: uuid.NewString(), ResourceId: uuid.NewString(), Kind: "test", ExecutionType: "test", ResourceType: "test", Resources: &commonv1.ExecutionResources{GpuIds: []int32{1,0}}}
	var wg sync.WaitGroup
	errors := make(chan error, 10)
	for i:=0; i<10; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); op, err := s.EnsureOperation(ctx, proto.Clone(req).(*operationv1.CreateOperationRequest)); if err == nil && op.GetOperationId()!=req.OperationId { t.Error("operation ID changed") }; errors<-err }()
	}
	wg.Wait(); close(errors)
	for err := range errors { if err != nil { t.Fatal(err) } }
	var count int
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM operation_outbox WHERE operation_id=$1`, req.OperationId).Scan(&count); err != nil || count!=1 {
		t.Fatalf("duplicate outbox: %d %v",count,err)
	}
	canonical := proto.Clone(req).(*operationv1.CreateOperationRequest)
	canonical.ResourceId = strings.ToUpper(req.ResourceId)
	canonical.Resources.GpuIds = []int32{0,1}
	canonical.Resources.GpuCount = 2
	canonical.MaxAttempts = 1
	if _, err := s.EnsureOperation(ctx, canonical); err != nil {
		t.Fatal("normalization replay:",err)
	}
	changed := proto.Clone(req).(*operationv1.CreateOperationRequest)
	changed.ResourceId = uuid.NewString()
	if _, err := s.EnsureOperation(ctx, changed); status.Code(err)!=codes.AlreadyExists {
		t.Fatalf("changed identity: %v",err)
	}
	if _, err := pool.Exec(ctx, `UPDATE operations SET status='succeeded',phase='completed',completed_at=NOW() WHERE operation_id=$1`, req.OperationId); err != nil {
		t.Fatal(err)
	}
	again, err := s.EnsureOperation(ctx, req)
	if err != nil || again.GetStatus()!="succeeded" {
		t.Fatalf("terminal replay: %v %v",again,err)
	}
	if err := pool.QueryRow(ctx, `SELECT count(*) FROM operation_outbox`).Scan(&count); err != nil || count!=1 {
		t.Fatal("terminal replay republished")
	}
	if _, err := pool.Exec(ctx, `UPDATE operations SET creation_request_hash=NULL WHERE operation_id=$1`, req.OperationId); err != nil {
		t.Fatal(err)
	}
	if _, err := s.EnsureOperation(ctx, req); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("legacy ID was adopted without a fingerprint: %v", err)
	}
	missing := proto.Clone(req).(*operationv1.CreateOperationRequest); missing.OperationId=""
	if _,err := s.EnsureOperation(ctx,missing); status.Code(err)!=codes.InvalidArgument {
		t.Fatalf("unreserved ID: %v",err)
	}
}
