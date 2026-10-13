package grpcclient

import (
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
)

type Clients struct {
	managementConn *grpc.ClientConn
	operationConn  *grpc.ClientConn
	Management     managementv1.ManagementServiceClient
	Operation      operationv1.OperationServiceClient
}

func Connect(managementAddress, operationAddress string) (*Clients, error) {
	mc, err := grpc.NewClient(managementAddress, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	oc, err := grpc.NewClient(operationAddress, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		mc.Close()
		return nil, err
	}
	return &Clients{managementConn: mc, operationConn: oc, Management: managementv1.NewManagementServiceClient(mc), Operation: operationv1.NewOperationServiceClient(oc)}, nil
}
func (c *Clients) Close() { _ = c.managementConn.Close(); _ = c.operationConn.Close() }
