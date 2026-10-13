package grpcclient

import (
	"context"

	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type Clients struct {
	managementConn *grpc.ClientConn
	operationConn  *grpc.ClientConn
	Management     managementv1.ManagementServiceClient
	Operation      operationv1.OperationServiceClient
}

func Connect(managementAddress, operationAddress string) (*Clients, error) {
	managementConn, err := grpc.NewClient(managementAddress, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	operationConn, err := grpc.NewClient(operationAddress, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		managementConn.Close()
		return nil, err
	}
	return &Clients{
		managementConn: managementConn,
		operationConn:  operationConn,
		Management:     managementv1.NewManagementServiceClient(managementConn),
		Operation:      operationv1.NewOperationServiceClient(operationConn),
	}, nil
}

func (c *Clients) Close() {
	_ = c.managementConn.Close()
	_ = c.operationConn.Close()
}

func (c *Clients) ModelReady(ctx context.Context, modelVersionID string) (bool, error) {
	mv, err := c.Management.GetModelVersion(ctx, &managementv1.GetModelVersionRequest{ModelVersionId: modelVersionID})
	if err != nil {
		return false, err
	}
	return mv.GetState() == "ready", nil
}
