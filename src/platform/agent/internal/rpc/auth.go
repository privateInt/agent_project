package rpc

import (
	"context"
	"crypto/subtle"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"strings"
)

func ValidID(value string) bool {
	id, err := uuid.Parse(value)
	return err == nil && id != uuid.Nil && id.String() == value
}
func NewID() string { return uuid.NewString() }
func Authorized(actual, token string) bool {
	return subtle.ConstantTimeCompare([]byte(actual), []byte("Bearer "+token)) == 1
}
func RequestID(ctx context.Context) string {
	md, _ := metadata.FromIncomingContext(ctx)
	if values := md.Get("x-request-id"); len(values) == 1 {
		return values[0]
	}
	return ""
}
func Outgoing(ctx context.Context, requestID string) context.Context {
	return metadata.AppendToOutgoingContext(ctx, "x-request-id", requestID)
}
func Authenticate(token string) grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, info *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
		md, _ := metadata.FromIncomingContext(ctx)
		values := md.Get("authorization")
		if len(values) != 1 || !Authorized(values[0], token) {
			return nil, status.Error(codes.Unauthenticated, "authentication required")
		}
		if !strings.HasPrefix(info.FullMethod, "/grpc.health.v1.Health/") && !ValidID(RequestID(ctx)) {
			return nil, status.Error(codes.InvalidArgument, "x-request-id must be a canonical UUID")
		}
		return handler(ctx, req)
	}
}

type Credentials struct{ Token string }

func (c Credentials) GetRequestMetadata(context.Context, ...string) (map[string]string, error) {
	return map[string]string{"authorization": "Bearer " + c.Token}, nil
}

// A02 transport is restricted to a private Docker network on a trusted host.
// Remote/multi-host deployment requires TLS before changing this contract.
func (Credentials) RequireTransportSecurity() bool { return false }
