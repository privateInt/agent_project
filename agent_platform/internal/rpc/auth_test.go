package rpc

import (
	"context"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"testing"
)

func TestInternalAuthenticationAndRequestID(t *testing.T) {
	for _, test := range []struct {
		auth, id, method string
		want             codes.Code
	}{
		{"", NewID(), "/agent.platform.v1.PlatformService/GetStatus", codes.Unauthenticated},
		{"Bearer token", "bad", "/agent.platform.v1.PlatformService/GetStatus", codes.InvalidArgument},
		{"Bearer token", NewID(), "/agent.platform.v1.PlatformService/GetStatus", codes.OK},
		{"Bearer token", "", "/grpc.health.v1.Health/Check", codes.OK},
	} {
		ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", test.auth, "x-request-id", test.id))
		_, err := Authenticate("token")(ctx, nil, &grpc.UnaryServerInfo{FullMethod: test.method}, func(context.Context, any) (any, error) { return nil, nil })
		if status.Code(err) != test.want {
			t.Fatalf("%s: %v", test.method, err)
		}
	}
}
