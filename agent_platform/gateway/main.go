package main

import (
	"agent-platform/gateway/app"
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/config"
	"agent-platform/internal/rpc"
	"context"
	"errors"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"log"
	"net/http"
	"net/http/httputil"
	"net/url"
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
	publicToken, err := config.Secret("AGENT_API_TOKEN_FILE")
	if err != nil {
		return err
	}
	internalToken, err := config.Secret("AGENT_INTERNAL_TOKEN_FILE")
	if err != nil {
		return err
	}
	conn, err := grpc.NewClient(config.Env("AGENT_SERVICE_ADDRESS", "agent-service:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: internalToken}), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(8<<20), grpc.MaxCallSendMsgSize(8<<20)))
	if err != nil {
		return err
	}
	defer conn.Close()
	mcpURL, err := url.Parse(config.Env("AGENT_MCP_URL", "http://agent-mcp:8000"))
	if err != nil {
		return err
	}
	proxy := httputil.NewSingleHostReverseProxy(mcpURL)
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		http.Error(w, "MCP service unavailable", http.StatusServiceUnavailable)
	}
	readyClient := &http.Client{Timeout: time.Second}
	mcpReady := func(ctx context.Context) bool {
		req, err := http.NewRequestWithContext(ctx, "GET", mcpURL.String()+"/healthz", nil)
		if err != nil {
			return false
		}
		res, err := readyClient.Do(req)
		if err != nil {
			return false
		}
		res.Body.Close()
		return res.StatusCode == 200
	}
	handler := (&app.Server{Client: platformv1.NewPlatformServiceClient(conn), Tools: platformv1.NewToolServiceClient(conn), Agents: platformv1.NewAgentServiceClient(conn), Token: publicToken, MCP: proxy, MCPReady: mcpReady}).Handler()
	server := &http.Server{Addr: config.Env("AGENT_HTTP_ADDRESS", ":8000"), Handler: handler, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 10 * time.Second, WriteTimeout: 45 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 16 << 10}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	done := make(chan struct{})
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
		close(done)
	}()
	log.Print("agent-gateway ready (A07)")
	err = server.ListenAndServe()
	if errors.Is(err, http.ErrServerClosed) {
		<-done
		return nil
	}
	return err
}
