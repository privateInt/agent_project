package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/internal/version"
	"context"
	"errors"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"testing"
)

type memory struct {
	records map[string]*platformv1.SystemCheck
	fail    bool
}

func (m *memory) Ping(context.Context) error {
	if m.fail {
		return errors.New("private DB error")
	}
	return nil
}
func (m *memory) Save(_ context.Context, c *platformv1.SystemCheck) error {
	if m.fail {
		return errors.New("private connection data")
	}
	m.records[c.CheckId] = c
	return nil
}
func (m *memory) Get(_ context.Context, id string) (*platformv1.SystemCheck, error) {
	if m.fail {
		return nil, errors.New("private DB error")
	}
	c, ok := m.records[id]
	if !ok {
		return nil, store.ErrNotFound
	}
	return c, nil
}

type runtimeStub struct {
	fail         bool
	incompatible bool
}

func (r runtimeStub) Probe(ctx context.Context, _ *emptypb.Empty, _ ...grpc.CallOption) (*platformv1.RuntimeProbe, error) {
	if r.fail {
		return nil, status.Error(codes.Unavailable, "private endpoint")
	}
	md, _ := metadata.FromOutgoingContext(ctx)
	contract := version.Contract
	if r.incompatible {
		contract = "next"
	}
	return &platformv1.RuntimeProbe{InstanceId: "fdb55e3e-9d54-4b68-96c2-8af914c25a3f", ContractVersion: contract, RequestId: md.Get("x-request-id")[0]}, nil
}
func setup() (*Service, context.Context) {
	s := &Service{DB: &memory{records: map[string]*platformv1.SystemCheck{}}, Runtime: runtimeStub{}}
	return s, metadata.NewIncomingContext(context.Background(), metadata.Pairs("x-request-id", rpc.NewID()))
}
func TestDiagnosticPersistsAndRetrievesOriginalRequest(t *testing.T) {
	s, ctx := setup()
	c, err := s.CreateSystemCheck(ctx, &emptypb.Empty{})
	if err != nil {
		t.Fatal(err)
	}
	if c.State != "succeeded" || c.RequestId != rpc.RequestID(ctx) || !rpc.ValidID(c.CheckId) {
		t.Fatal(c)
	}
	_, other := setup()
	got, err := s.GetSystemCheck(other, &platformv1.GetSystemCheckRequest{CheckId: c.CheckId})
	if err != nil || got.RequestId != c.RequestId || got.RuntimeInstanceId == "" {
		t.Fatalf("lost diagnostic identity: %v %v", got, err)
	}
}
func TestRuntimeFailureRecordedAndReadinessDegraded(t *testing.T) {
	for _, incompatible := range []bool{false, true} {
		s, ctx := setup()
		s.Runtime = runtimeStub{fail: !incompatible, incompatible: incompatible}
		c, err := s.CreateSystemCheck(ctx, &emptypb.Empty{})
		if err != nil || c.State != "failed" || c.ErrorCode == "" {
			t.Fatalf("%v %v", c, err)
		}
		status, err := s.GetStatus(ctx, &emptypb.Empty{})
		if err != nil || status.Ready || status.Database != "ready" {
			t.Fatalf("%v %v", status, err)
		}
		if _, err = s.GetSystemCheck(ctx, &platformv1.GetSystemCheckRequest{CheckId: c.CheckId}); err != nil {
			t.Fatal(err)
		}
	}
}
func TestDatabaseFailureDoesNotReturnSuccessfulCreation(t *testing.T) {
	s, ctx := setup()
	s.DB.(*memory).fail = true
	if c, err := s.CreateSystemCheck(ctx, &emptypb.Empty{}); c != nil || status.Code(err) != codes.Unavailable {
		t.Fatalf("%v %v", c, err)
	}
	got, err := s.GetStatus(ctx, &emptypb.Empty{})
	if err != nil || got.Ready || got.SchemaVersion != 0 {
		t.Fatalf("%v %v", got, err)
	}
}
func TestLookupValidationAndMissing(t *testing.T) {
	s, ctx := setup()
	for _, item := range []struct {
		id   string
		code codes.Code
	}{{"bad", codes.InvalidArgument}, {rpc.NewID(), codes.NotFound}} {
		_, err := s.GetSystemCheck(ctx, &platformv1.GetSystemCheckRequest{CheckId: item.id})
		if status.Code(err) != item.code {
			t.Fatal(err)
		}
	}
}
