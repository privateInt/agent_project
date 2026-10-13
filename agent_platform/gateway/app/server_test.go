package app

import (
	platformv1 "agent-platform/gen/agent/platform/v1"
	"agent-platform/internal/rpc"
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const token = "0123456789abcdef0123456789abcdef"

type backend struct {
	platformv1.UnimplementedPlatformServiceServer
}

func (backend) GetStatus(ctx context.Context, _ *emptypb.Empty) (*platformv1.PlatformStatus, error) {
	return &platformv1.PlatformStatus{Ready: true, RequestId: rpc.RequestID(ctx)}, nil
}
func (backend) CreateSystemCheck(ctx context.Context, _ *emptypb.Empty) (*platformv1.SystemCheck, error) {
	return &platformv1.SystemCheck{CheckId: rpc.NewID(), RequestId: rpc.RequestID(ctx), State: "succeeded"}, nil
}
func (backend) GetSystemCheck(context.Context, *platformv1.GetSystemCheckRequest) (*platformv1.SystemCheck, error) {
	return nil, status.Error(codes.Unavailable, "postgres://private:secret@host/db")
}
func handler(t *testing.T) http.Handler {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer(grpc.UnaryInterceptor(rpc.Authenticate(token)))
	platformv1.RegisterPlatformServiceServer(server, backend{})
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///buffer", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithPerRPCCredentials(rpc.Credentials{Token: token}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return (&Server{Client: platformv1.NewPlatformServiceClient(conn), Token: token}).Handler()
}
func TestHTTPGRPCRequestCorrelationAndPersistedResponse(t *testing.T) {
	h := handler(t)
	id := rpc.NewID()
	for _, item := range []struct {
		method, path, body string
		code               int
	}{{"GET", "/api/status", "", 200}, {"POST", "/api/system-checks", "{}", 201}} {
		r := httptest.NewRequest(item.method, item.path, strings.NewReader(item.body))
		r.Header.Set("Authorization", "Bearer "+token)
		r.Header.Set("X-Request-ID", id)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != item.code || w.Header().Get("X-Request-ID") != id || !strings.Contains(w.Body.String(), id) {
			t.Fatalf("%d %s", w.Code, w.Body)
		}
		if item.code == 201 && !strings.HasPrefix(w.Header().Get("Location"), "/api/system-checks/") {
			t.Fatal("missing lookup URL")
		}
	}
}
func TestPublicHealthAndAuthenticatedAPIs(t *testing.T) {
	h := handler(t)
	for _, item := range []struct {
		path string
		code int
	}{{"/healthz", 200}, {"/readyz", 200}, {"/api/status", 401}, {"/api/system-checks/" + rpc.NewID(), 401}} {
		w := httptest.NewRecorder()
		h.ServeHTTP(w, httptest.NewRequest("GET", item.path, nil))
		if w.Code != item.code {
			t.Fatalf("%s: %d", item.path, w.Code)
		}
	}
}
func TestInvalidInputsDoNotReachBackend(t *testing.T) {
	h := handler(t)
	for _, item := range []struct {
		body, id string
		code     int
	}{{"{}", "bad", 400}, {"{\"tool\":1}", rpc.NewID(), 400}, {"null", rpc.NewID(), 400}, {strings.Repeat("x", 1025), rpc.NewID(), 413}} {
		r := httptest.NewRequest("POST", "/api/system-checks", strings.NewReader(item.body))
		r.Header.Set("Authorization", "Bearer "+token)
		r.Header.Set("X-Request-ID", item.id)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != item.code {
			t.Fatalf("want %d got %d", item.code, w.Code)
		}
	}
}
func TestBackendErrorIsRedacted(t *testing.T) {
	h := handler(t)
	r := httptest.NewRequest("GET", "/api/system-checks/"+rpc.NewID(), nil)
	r.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 503 || strings.Contains(w.Body.String(), "private") || strings.Contains(w.Body.String(), "secret") {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
}
