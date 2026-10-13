package app

import (
	"context"
	"strings"
	"time"

	rerankingv1 "knowledge-platform/gen/model/reranking/v1"
	"knowledge-platform/internal/retrieval"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Preserve useful Model codes, never forward its raw error message or input.
func rerankModelError(ctx context.Context,err error)error{
	if ctx.Err()!=nil{return status.FromContextError(ctx.Err()).Err()}
	code:=strings.SplitN(status.Convert(err).Message(),":",2)[0]
	known:=map[string]struct{grpc codes.Code;message string}{
		"RERANK_INVALID_REQUEST":{codes.InvalidArgument,"Model rejected rerank input"},
		"RERANK_INPUT_TOO_LARGE":{codes.InvalidArgument,"query and candidates exceed the Model byte limit"},
		"RERANK_INPUT_TOO_LONG":{codes.InvalidArgument,"query and a candidate exceed the Model pair token limit"},
		"RERANK_INPUT_REJECTED":{codes.InvalidArgument,"Model rejected a query and candidate pair"},
		"RERANK_NOT_READY":{codes.FailedPrecondition,"reranker deployment is not ready"},
		"RERANK_UNSUPPORTED_PROFILE":{codes.FailedPrecondition,"reranker profile is not supported"},
		"RERANK_PROFILE_MISMATCH":{codes.FailedPrecondition,"reranker profile changed"},
		"RERANK_IDENTITY_MISMATCH":{codes.FailedPrecondition,"reranker runtime identity changed"},
		"RERANK_INVALID_RESPONSE":{codes.DataLoss,"Model scoring response could not be verified"},
		"RERANK_UNAVAILABLE":{codes.Unavailable,"reranker is unavailable"},
		"RERANK_TIMEOUT":{codes.DeadlineExceeded,"reranker did not finish before deadline"},
	}
	if safe,ok:=known[code];ok&&safe.grpc==status.Code(err){return status.Error(safe.grpc,code+": "+safe.message)}
	switch status.Code(err){
	case codes.NotFound:return missing("SEARCH_RERANK_NOT_FOUND: reranker deployment was not found")
	case codes.DeadlineExceeded:return status.Error(codes.DeadlineExceeded,"RERANK_TIMEOUT: reranker deadline exceeded")
	case codes.Canceled:return status.Error(codes.Canceled,"SEARCH_CANCELLED: reranker request cancelled")
	case codes.FailedPrecondition:return precondition("SEARCH_RERANK_UNAVAILABLE: reranker is not ready or its identity changed")
	case codes.InvalidArgument:return invalid("RERANK_INPUT_REJECTED: Model rejected the candidate inputs")
	case codes.DataLoss, codes.ResourceExhausted:return corrupt("SEARCH_RERANK_RESPONSE_INVALID: Model response could not be verified")
	default:return unavailable("RERANK_UNAVAILABLE: Model reranker request failed")
	}
}

func(s *IndexService)rerankProfile(ctx context.Context,q retrieval.Query)(*rerankingv1.RerankerProfile,error){
	if s.reranker==nil{return nil,unavailable("SEARCH_RERANK_NOT_CONFIGURED: configure the Model reranking connection")}
	call,cancel:=context.WithTimeout(ctx,5*time.Second);defer cancel()
	p,err:=s.reranker.GetProfile(call,&rerankingv1.GetProfileRequest{DeploymentId:q.Rerank.DeploymentID},grpc.MaxCallRecvMsgSize(256<<10))
	if err!=nil{return nil,rerankModelError(call,err)}
	if err=retrieval.ValidateReranker(p,q.Rerank);err!=nil{return nil,searchInputError(err)}
	if q.CandidateK>int(p.MaxCandidates){return nil,invalid("SEARCH_INVALID_REQUEST: candidate_k exceeds Model limit")}
	return proto.Clone(p).(*rerankingv1.RerankerProfile),nil
}

func(s *IndexService)rerankCandidates(ctx context.Context,q retrieval.Query,candidates retrieval.Candidates,p *rerankingv1.RerankerProfile)(*rerankingv1.RerankResponse,error){
	req,err:=retrieval.RerankRequest(q,candidates,p);if err!=nil{return nil,searchInputError(err)}
	if len(req.Candidates)==0{return &rerankingv1.RerankResponse{Profile:p},nil}
	call,cancel:=context.WithTimeout(ctx,45*time.Second);defer cancel()
	out,err:=s.reranker.Rerank(call,req,grpc.MaxCallRecvMsgSize(256<<10))
	if err!=nil{return nil,rerankModelError(call,err)}
	return out,nil
}
