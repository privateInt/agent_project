package main

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"sync"
	"testing"

	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	indexingv1 "knowledge-platform/gen/indexing/v1"
	embeddingv1 "knowledge-platform/gen/model/embedding/v1"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/indexing"
)

// Exercise run(), including real gRPC serialization and its retry loop, against
// loopback servers. No deployment, GPU, OpenSearch or catalog is used here.
type jobState struct {
	mu                                   sync.Mutex
	v                                    indexing.Version
	mode                                 string
	embeds, commits, finishes, successes int
	failures                             []*operationv1.FailOperationRequest
	grant                                *indexingv1.ExecutionRequest
	receipts                             map[int32]*indexingv1.CommitBatchRequest
}
type jobOperations struct {
	operationv1.UnimplementedOperationServiceServer
	s *jobState
}
type jobIndexing struct {
	indexingv1.UnimplementedIndexingServiceServer
	s *jobState
}
type jobModel struct {
	embeddingv1.UnimplementedEmbeddingServiceServer
	s *jobState
}

func (f *jobOperations) BeginExecution(_ context.Context, r *operationv1.BeginExecutionRequest) (*operationv1.Operation, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.grant = &indexingv1.ExecutionRequest{IndexVersionId: r.ResourceId, OperationId: r.OperationId, DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId}
	return &operationv1.Operation{Status: "running", DispatchToken: r.DispatchToken, ExecutionInstanceId: r.ExecutionInstanceId}, nil
}
func (f *jobOperations) UpdatePhase(context.Context, *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
	return &operationv1.Operation{}, nil
}
func (f *jobOperations) FailOperation(_ context.Context, r *operationv1.FailOperationRequest) (*operationv1.Operation, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.failures = append(f.s.failures, proto.Clone(r).(*operationv1.FailOperationRequest))
	return &operationv1.Operation{Status: "failed"}, nil
}
func (f *jobOperations) SucceedOperation(context.Context, *operationv1.UpdateOperationRequest) (*operationv1.Operation, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.successes++
	return &operationv1.Operation{Status: "succeeded"}, nil
}
func jobJSON(v any) (*indexingv1.JSONResponse, error) {
	b, e := json.Marshal(v)
	return &indexingv1.JSONResponse{Json: b}, e
}
func (f *jobIndexing) PrepareBuild(_ context.Context, r *indexingv1.ExecutionRequest) (*indexingv1.JSONResponse, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if !proto.Equal(r, f.s.grant) {
		return nil, status.Error(codes.FailedPrecondition, "grant differs")
	}
	return jobJSON(f.s.v)
}
func (f *jobIndexing) ReadBatch(_ context.Context, r *indexingv1.ReadBatchRequest) (*indexingv1.JSONResponse, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	if !proto.Equal(r.Execution, f.s.grant) || int(r.Offset) != f.s.v.Next {
		return nil, status.Error(codes.FailedPrecondition, "offset or grant differs")
	}
	id := strings.Repeat("a", 64)
	if r.Offset == 1 {
		id = strings.Repeat("b", 64)
	}
	return jobJSON(indexing.Batch{Version: f.s.v, Offset: int(r.Offset), Chunks: []chunking.Chunk{{ID: id, Text: "isolated test paragraph"}}})
}
func (f *jobIndexing) CommitBatch(_ context.Context, r *indexingv1.CommitBatchRequest) (*indexingv1.JSONResponse, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.commits++
	if !proto.Equal(r.Execution, f.s.grant) {
		return nil, status.Error(codes.FailedPrecondition, "grant differs")
	}
	if old, ok := f.s.receipts[r.Offset]; ok {
		if !proto.Equal(old, r) {
			return nil, status.Error(codes.FailedPrecondition, "replay changed the inference receipt")
		}
	} else {
		if int(r.Offset) != f.s.v.Next {
			return nil, status.Error(codes.FailedPrecondition, "offset differs")
		}
		f.s.receipts[r.Offset] = proto.Clone(r).(*indexingv1.CommitBatchRequest)
		f.s.v.Next += len(r.Result.Data) // durable before the response is lost
	}
	if f.s.mode == "commit_lost_always" || f.s.mode == "commit_lost_once" && f.s.commits == 1 {
		return nil, status.Error(codes.Unavailable, "commit reply lost")
	}
	return jobJSON(f.s.v)
}
func (f *jobIndexing) FinishBuild(_ context.Context, r *indexingv1.ExecutionRequest) (*indexingv1.JSONResponse, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.finishes++
	if !proto.Equal(r, f.s.grant) || f.s.v.Next != f.s.v.Count {
		return nil, status.Error(codes.FailedPrecondition, "incomplete")
	}
	f.s.v.State = "ready" // reconciliation must preserve this durable receipt
	if f.s.mode == "finish_lost_always" || f.s.mode == "finish_lost_once" && f.s.finishes == 1 {
		return nil, status.Error(codes.Unavailable, "finish reply lost")
	}
	return jobJSON(f.s.v)
}
func (f *jobModel) Embed(_ context.Context, r *embeddingv1.EmbedRequest) (*embeddingv1.EmbedResponse, error) {
	f.s.mu.Lock()
	defer f.s.mu.Unlock()
	f.s.embeds++
	if r.Role != "document" || r.DeploymentId != f.s.v.Profile.DeploymentId || r.ExpectedEmbeddingSpaceId != f.s.v.Profile.EmbeddingSpaceId {
		return nil, status.Error(codes.InvalidArgument, "wrong embedding identity")
	}
	if f.s.mode == "model_unavailable" || f.s.mode == "second_model_unavailable" && f.s.embeds == 2 {
		return nil, status.Error(codes.Unavailable, "test model unavailable")
	}
	out := &embeddingv1.EmbedResponse{Profile: proto.Clone(f.s.v.Profile).(*embeddingv1.EmbeddingProfile), Role: r.Role}
	for i, input := range r.Inputs {
		vector := make([]float64, 1024)
		vector[0] = 1
		out.Data = append(out.Data, &embeddingv1.EmbeddingOutput{Id: input.Id, Index: int32(i), Embedding: vector, InputTokens: 8})
	}
	if f.s.mode == "invalid_model_receipt" {
		out.Data[0].Id = "wrong"
	}
	return out, nil
}

func TestIndexJobFailureRecovery(t *testing.T) {
	for _, tt := range []struct {
		mode, failCode                             string
		embeds, commits, finishes, next, successes int
	}{
		{"model_unavailable", "MODEL_EMBEDDING_FAILED", 1, 0, 0, 0, 0},
		{"second_model_unavailable", "MODEL_EMBEDDING_FAILED", 2, 1, 0, 1, 0},
		{"invalid_model_receipt", "MODEL_RECEIPT_INVALID", 1, 0, 0, 0, 0},
		{"commit_lost_once", "", 2, 3, 1, 2, 1},
		{"commit_lost_always", "INDEX_BATCH_UNCONFIRMED", 1, 3, 0, 1, 0},
		{"finish_lost_once", "", 2, 2, 2, 2, 1},
		{"finish_lost_always", "", 2, 2, 3, 2, 0},
	} {
		t.Run(tt.mode, func(t *testing.T) {
			deployment := uuid.NewString()
			p := &embeddingv1.EmbeddingProfile{DeploymentId: deployment, ModelVersionId: uuid.NewString(), ProfileId: "snowflake-ko-v1", EmbeddingSpaceId: "es_" + strings.Repeat("a", 64), SourceManifestSha256: strings.Repeat("b", 64), TokenizerSha256: strings.Repeat("c", 64), RuntimeImageId: "sha256:" + strings.Repeat("d", 64), RuntimeContainerId: strings.Repeat("e", 64), ServedModelName: "deployment-" + deployment, RepoId: "dragonkue/snowflake-arctic-embed-l-v2.0-ko", QueryPrefix: "query: ", Pooling: "cls", Normalization: "l2", Dimension: 1024, MaxInputTokens: 2048, MaxBatchSize: 1, MaxInputBytes: 131072, Truncation: "reject", Capability: "embedding", Status: "ready"}
			s := &jobState{mode: tt.mode, v: indexing.Version{ID: uuid.NewString(), OperationID: uuid.NewString(), Profile: p, State: "indexing", Count: 2}, receipts: map[int32]*indexingv1.CommitBatchRequest{}}
			listener, e := net.Listen("tcp", "127.0.0.1:0")
			if e != nil {
				t.Fatal(e)
			}
			server := grpc.NewServer()
			operationv1.RegisterOperationServiceServer(server, &jobOperations{s: s})
			indexingv1.RegisterIndexingServiceServer(server, &jobIndexing{s: s})
			embeddingv1.RegisterEmbeddingServiceServer(server, &jobModel{s: s})
			go func() { _ = server.Serve(listener) }()
			t.Cleanup(func() { server.Stop(); _ = listener.Close() })
			for _, key := range []string{"OPERATION_GRPC_ADDRESS", "KNOWLEDGE_GRPC_ADDRESS", "MODEL_EMBEDDING_GRPC_ADDRESS"} {
				t.Setenv(key, listener.Addr().String())
			}
			t.Setenv("OPERATION_ID", s.v.OperationID)
			t.Setenv("RESOURCE_ID", s.v.ID)
			t.Setenv("DISPATCH_TOKEN", uuid.NewString())
			e = run()
			s.mu.Lock()
			defer s.mu.Unlock()
			if (e == nil) != (tt.successes == 1) {
				t.Fatalf("unexpected run outcome: %v", e)
			}
			if s.embeds != tt.embeds || s.commits != tt.commits || s.finishes != tt.finishes || s.v.Next != tt.next || s.successes != tt.successes {
				t.Fatalf("embeds=%d commits=%d finishes=%d next=%d successes=%d", s.embeds, s.commits, s.finishes, s.v.Next, s.successes)
			}
			if tt.failCode == "" {
				if len(s.failures) != 0 {
					t.Fatal("uncertain final receipt overwritten by failure", s.failures)
				}
			} else {
				if len(s.failures) != 1 {
					t.Fatal("failure report count", len(s.failures))
				}
				f := s.failures[0]
				if f.ErrorCode != tt.failCode || f.Retryable || f.OperationId != s.v.OperationID || f.DispatchToken != s.grant.DispatchToken || f.ExecutionInstanceId != s.grant.ExecutionInstanceId {
					t.Fatal("failure grant or policy differs", f)
				}
			}
			if strings.HasPrefix(tt.mode, "finish_lost") && s.v.State != "ready" {
				t.Fatal("durable ready receipt lost")
			}
		})
	}
}
