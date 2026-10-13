package main

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"agent-platform/mcp/app"
	"context"
	"errors"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"net/http"
	"os/signal"
	"strings"
	"syscall"
	"time"
)

func main() {
	if err := run(); err != nil {
		log.Fatal(err)
	}
}
func run() error {
	token, err := config.Secret("AGENT_API_TOKEN_FILE")
	if err != nil {
		return err
	}
	internal, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(config.Env("AGENT_SERVICE_ADDRESS", "agent-service:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: internal}), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(8<<20), grpc.MaxCallSendMsgSize(8<<20)))
	if err != nil {
		return err
	}
	defer conn.Close()
	handler := (&app.Server{Client: platformv1.NewToolServiceClient(conn), Token: token, AllowedOrigins: strings.Fields(config.Env("AGENT_MCP_ALLOWED_ORIGINS", ""))}).Handler()
	server := &http.Server{Addr: config.Env("AGENT_MCP_ADDRESS", ":8000"), Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 40 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	done := make(chan struct{})
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
		close(done)
	}()
	log.Print("agent-mcp ready (A07)")
	err = server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		<-done
		return nil
	}
	return err
}
