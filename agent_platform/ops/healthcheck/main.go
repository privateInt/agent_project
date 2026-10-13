package main

import (
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"net/http"
	"os"
	"time"
)

func main() {
	if !healthy() {
		os.Exit(1)
	}
}
func healthy() bool {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	if len(os.Args) == 2 && os.Args[1] == "http" {
		req, err := http.NewRequestWithContext(ctx, "GET", "http://127.0.0.1:8000/healthz", nil)
		if err != nil {
			return false
		}
		res, err := http.DefaultClient.Do(req)
		if err != nil {
			return false
		}
		res.Body.Close()
		return res.StatusCode == 200
	}
	token, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		return false
	}
	conn, err := grpc.NewClient("127.0.0.1:9000", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		return false
	}
	defer conn.Close()
	out, err := healthpb.NewHealthClient(conn).Check(ctx, &healthpb.HealthCheckRequest{})
	return err == nil && out.Status == healthpb.HealthCheckResponse_SERVING
}
