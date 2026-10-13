package rpc

import (
	"context"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
	"time"
)

type Health struct {
	healthpb.UnimplementedHealthServer
	ServiceName string
	Ping        func(context.Context) error
}

func (h *Health) Check(ctx context.Context, in *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	if in.Service != "" && in.Service != h.ServiceName {
		return nil, status.Error(codes.NotFound, "unknown service")
	}
	ctx, cancel := context.WithTimeout(ctx, time.Second)
	defer cancel()
	result := healthpb.HealthCheckResponse_SERVING
	if err := h.Ping(ctx); err != nil {
		result = healthpb.HealthCheckResponse_NOT_SERVING
	}
	return &healthpb.HealthCheckResponse{Status: result}, nil
}
