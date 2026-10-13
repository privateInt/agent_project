package grpcclient

import (
	deploymentv1 "model-platform/gen/deployment/v1"
    embeddingv1 "model-platform/gen/embedding/v1"
    rerankingv1 "model-platform/gen/reranking/v1"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type Clients struct {
	connections []*grpc.ClientConn
	Management  managementv1.ManagementServiceClient
	Operation   operationv1.OperationServiceClient
	Training    trainingv1.TrainingServiceClient
	Deployment  deploymentv1.DeploymentServiceClient
    Embedding embeddingv1.EmbeddingServiceClient
    Reranking rerankingv1.RerankingServiceClient
}

func Connect(managementAddress, operationAddress, trainingAddress, deploymentAddress string) (*Clients, error) {
	connect := func(address string) (*grpc.ClientConn, error) {
		return grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	}
	mc, err := connect(managementAddress)
	if err != nil {
		return nil, err
	}
	oc, err := connect(operationAddress)
	if err != nil {
		mc.Close()
		return nil, err
	}
	tc, err := connect(trainingAddress)
	if err != nil {
		mc.Close()
		oc.Close()
		return nil, err
	}
	dc, err := connect(deploymentAddress)
	if err != nil {
		mc.Close()
		oc.Close()
		tc.Close()
		return nil, err
	}
	return &Clients{
		connections: []*grpc.ClientConn{mc, oc, tc, dc},
		Management:  managementv1.NewManagementServiceClient(mc),
		Operation:   operationv1.NewOperationServiceClient(oc),
		Training:    trainingv1.NewTrainingServiceClient(tc),
		Deployment:  deploymentv1.NewDeploymentServiceClient(dc),
        Embedding: embeddingv1.NewEmbeddingServiceClient(dc),
        Reranking: rerankingv1.NewRerankingServiceClient(dc),
	}, nil
}

func (c *Clients) Close() {
	for _, conn := range c.connections {
		_ = conn.Close()
	}
}
