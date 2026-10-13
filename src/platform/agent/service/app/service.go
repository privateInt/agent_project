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
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	"log/slog"
	"time"
)

type Repository interface {
	Ping(context.Context) error
	Save(context.Context, *platformv1.SystemCheck) error
	Get(context.Context, string) (*platformv1.SystemCheck, error)
}
type ProbeClient interface {
	Probe(context.Context, *emptypb.Empty, ...grpc.CallOption) (*platformv1.RuntimeProbe, error)
}
type Service struct {
	platformv1.UnimplementedPlatformServiceServer
	DB      Repository
	Runtime ProbeClient
}

func (s *Service) probe(ctx context.Context) (*platformv1.RuntimeProbe, error) {
	ctx, cancel := context.WithTimeout(ctx, 2*time.Second)
	defer cancel()
	p, err := s.Runtime.Probe(rpc.Outgoing(ctx, rpc.RequestID(ctx)), &emptypb.Empty{}, grpc.WaitForReady(false))
	if err != nil {
		return nil, err
	}
	if p.ContractVersion != version.Contract || !rpc.ValidID(p.InstanceId) || p.RequestId != rpc.RequestID(ctx) {
		return nil, status.Error(codes.FailedPrecondition, "runtime contract mismatch")
	}
	return p, nil
}
func (s *Service) GetStatus(ctx context.Context, _ *emptypb.Empty) (*platformv1.PlatformStatus, error) {
	out := &platformv1.PlatformStatus{Platform: "agent", Version: version.Build, Stage: version.Stage, Database: "ready", Runtime: "ready", SchemaVersion: version.Schema, RequestId: rpc.RequestID(ctx)}
	dbctx, cancel := context.WithTimeout(ctx, time.Second)
	err := s.DB.Ping(dbctx)
	cancel()
	if err != nil {
		out.Database = "unavailable"
		out.SchemaVersion = 0
	}
	if _, err = s.probe(ctx); err != nil {
		out.Runtime = "unavailable"
		if status.Code(err) == codes.FailedPrecondition {
			out.Runtime = "incompatible"
		}
	}
	out.Ready = out.Database == "ready" && out.Runtime == "ready"
	return out, nil
}
func (s *Service) CreateSystemCheck(ctx context.Context, _ *emptypb.Empty) (*platformv1.SystemCheck, error) {
	// PostgreSQL timestamptz and pgx preserve microseconds. Use the same
	// precision in the creation response so a later lookup is identical.
	started := time.Now().UTC().Truncate(time.Microsecond)
	c := &platformv1.SystemCheck{CheckId: rpc.NewID(), RequestId: rpc.RequestID(ctx), State: "succeeded", CreatedAt: timestamppb.New(started)}
	p, err := s.probe(ctx)
	if err != nil {
		c.State = "failed"
		c.ErrorCode = "RUNTIME_UNAVAILABLE"
		if status.Code(err) == codes.FailedPrecondition {
			c.ErrorCode = "RUNTIME_CONTRACT_MISMATCH"
		}
	} else {
		c.RuntimeInstanceId = p.InstanceId
	}
	completed := time.Now().UTC().Truncate(time.Microsecond)
	if completed.Before(started) {
		completed = started
	}
	c.CompletedAt = timestamppb.New(completed)
	if err = s.DB.Save(ctx, c); err != nil {
		return nil, status.Error(codes.Unavailable, "diagnostic could not be persisted")
	}
	slog.Info("system_check_completed", "check_id", c.CheckId, "request_id", c.RequestId, "state", c.State, "error_code", c.ErrorCode)
	return c, nil
}
func (s *Service) GetSystemCheck(ctx context.Context, in *platformv1.GetSystemCheckRequest) (*platformv1.SystemCheck, error) {
	if !rpc.ValidID(in.CheckId) {
		return nil, status.Error(codes.InvalidArgument, "check_id must be a canonical UUID")
	}
	c, err := s.DB.Get(ctx, in.CheckId)
	if errors.Is(err, store.ErrNotFound) {
		return nil, status.Error(codes.NotFound, "system check not found")
	}
	if err != nil {
		return nil, status.Error(codes.Unavailable, "Agent database unavailable")
	}
	return c, nil
}
