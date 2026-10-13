package app

import (
	"context"
 "github.com/google/uuid"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	operationv1 "data-platform/gen/operation/v1"
)

type OperationClient struct {
 owner string
	conn   *grpc.ClientConn
	client operationv1.OperationServiceClient
}

func ConnectOperation(address string) (*OperationClient, error) {
	conn, err := grpc.NewClient(address, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	return &OperationClient{owner: uuid.NewString(), conn: conn, client: operationv1.NewOperationServiceClient(conn)}, nil
}
func (c *OperationClient) Close() error { return c.conn.Close() }
func (c *OperationClient) Claim(ctx context.Context, id string) (*operationv1.ClaimOperationResponse, error) {
	return c.client.ClaimOperation(ctx, &operationv1.ClaimOperationRequest{OperationId: id, OwnerId:c.owner})
}
func(c *OperationClient)Fail(ctx context.Context,id,token,phase,code,message string,retryable bool)error{
 _,err:=c.client.FailDispatch(ctx,&operationv1.FailDispatchRequest{OperationId:id,DispatchToken:token,ErrorCode:code,ErrorMessage:message})
 return err
}
func(c *OperationClient)Get(ctx context.Context,id string)(*operationv1.Operation,error){return c.client.GetOperation(ctx,&operationv1.GetOperationRequest{OperationId:id})}
