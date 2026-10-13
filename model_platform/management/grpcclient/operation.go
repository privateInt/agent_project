package grpcclient

import (
	"context"
	"fmt"

	operationv1 "model-platform/gen/operation/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
)

type Operation struct {
	conn   *grpc.ClientConn
	client operationv1.OperationServiceClient
	health healthpb.HealthClient
}

func ConnectOperation(address string) (*Operation, error) {
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	return &Operation{conn: conn, client: operationv1.NewOperationServiceClient(conn), health: healthpb.NewHealthClient(conn)}, nil
}
func (o *Operation) Close() error { return o.conn.Close() }
func (o *Operation) Ready(ctx context.Context) error {
	res, err := o.health.Check(ctx, &healthpb.HealthCheckRequest{Service: operationv1.OperationService_ServiceDesc.ServiceName})
	if err != nil {
		return err
	}
	if res.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		return fmt.Errorf("operation service not serving: %s", res.GetStatus())
	}
	return nil
}
func (o *Operation) Create(ctx context.Context, req *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	return o.client.CreateOperation(ctx, req)
}

// The new RPC fails with Unimplemented on an old server instead of ignoring
// the domain's reserved Operation ID and creating duplicate Jobs.
func (o *Operation) Ensure(ctx context.Context, req *operationv1.CreateOperationRequest) (*operationv1.Operation, error) {
	return o.client.EnsureOperation(ctx, req)
}

func (o *Operation) Get(ctx context.Context, id string) (*operationv1.Operation, error) {
	return o.client.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: id})
}
