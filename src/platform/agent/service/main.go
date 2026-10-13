package main

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"agent-platform/internal/store"
	"agent-platform/service/app"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"log"
	"net"
	"os/signal"
	"syscall"
	"time"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
func run() error {
	token, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		return err
	}
	startup, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	db, err := store.Connect(startup)
	if err != nil {
		return err
	}
	defer db.Close()
	conn, err := grpc.NewClient(config.Env("AGENT_RUNTIME_ADDRESS", "agent-runtime:9001"), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		return err
	}
	defer conn.Close()
	listener, err := net.Listen("tcp", config.Env("AGENT_GRPC_ADDRESS", ":9000"))
	if err != nil {
		return err
	}
	defer listener.Close()
	server := grpc.NewServer(grpc.UnaryInterceptor(rpc.Authenticate(token)), grpc.MaxRecvMsgSize(8<<20), grpc.MaxSendMsgSize(8<<20))
	platformv1.RegisterPlatformServiceServer(server, &app.Service{DB: db, Runtime: platformv1.NewRuntimeServiceClient(conn)})
	platformv1.RegisterAgentServiceServer(server, &app.Agents{DB: db, Runtime: platformv1.NewRuntimeServiceClient(conn)})
	platformv1.RegisterToolServiceServer(server, &app.Tools{DB: db, Runtime: platformv1.NewRuntimeServiceClient(conn)})
	healthpb.RegisterHealthServer(server, &rpc.Health{ServiceName: platformv1.PlatformService_ServiceDesc.ServiceName, Ping: db.Ping})
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	workerDone := make(chan struct{})
	go func() {
		defer close(workerDone)
		(&app.ToolWorker{DB: db, Runtime: platformv1.NewRuntimeServiceClient(conn)}).Run(ctx)
	}()
	agentDone := make(chan struct{})
	go func() {
		defer close(agentDone)
		(&app.AgentWorker{DB: db, Runtime: platformv1.NewRuntimeServiceClient(conn)}).Run(ctx)
	}()
	defer func() { stop(); <-workerDone; <-agentDone }()
	go func() {
		<-ctx.Done()
		timer := time.AfterFunc(5*time.Second, server.Stop)
		defer timer.Stop()
		server.GracefulStop()
	}()
	log.Print("agent-service ready (A07)")
	return server.Serve(listener)
}
