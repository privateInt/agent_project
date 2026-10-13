package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"os"
	"testing"
	"time"
)

// Optional real Go/Python wire test. The repository is a test double; this does
// not claim PostgreSQL or container acceptance.
func TestPythonRuntimeContract(t *testing.T) {
	address := os.Getenv("AGENT_TEST_RUNTIME_ADDRESS")
	if address == "" {
		t.Skip("requires a running Python Agent runtime")
	}
	token, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	s, ctx := setup()
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	s.Runtime = platformv1.NewRuntimeServiceClient(conn)
	check, err := s.CreateSystemCheck(ctx, &emptypb.Empty{})
	if err != nil || check.State != "succeeded" {
		t.Fatalf("wire diagnostic: %v %v", check, err)
	}
	probe, err := s.GetStatus(ctx, &emptypb.Empty{})
	if err != nil || !probe.Ready {
		t.Fatalf("wire readiness: %v %v", probe, err)
	}
	bad, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	defer bad.Close()
	_, err = platformv1.NewRuntimeServiceClient(bad).Probe(rpc.Outgoing(ctx, rpc.NewID()), &emptypb.Empty{})
	if status.Code(err) != codes.Unauthenticated {
		t.Fatalf("Python authentication: %v", err)
	}
}
