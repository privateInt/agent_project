package app

import (
    "context"
    "encoding/json"
    "strings"
    "time"
    "unicode/utf8"

    rerankingv1 "model-platform/gen/reranking/v1"
    "model-platform/internal/reranking"
    "github.com/gofiber/fiber/v3"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/encoding/protojson"
    "google.golang.org/protobuf/proto"
)

func parseRerank(raw []byte)(*rerankingv1.RerankRequest,error){
    invalid:=func()(*rerankingv1.RerankRequest,error){return nil,status.Error(codes.InvalidArgument,"RERANK_INVALID_REQUEST: expected profile, query and id/text candidates required; no other fields allowed")}
    if len(raw)>1<<20 || !utf8.Valid(raw){return invalid()}
    object,err:=uniqueEvaluationObject(raw)
    if err!=nil || len(object)!=3 || object["expected_reranker_profile_id"]==nil || object["query"]==nil || object["candidates"]==nil{return invalid()}
    var body struct{Profile string `json:"expected_reranker_profile_id"`;Query string `json:"query"`;Candidates []json.RawMessage `json:"candidates"`}
    if json.Unmarshal(raw,&body)!=nil{return invalid()}
    req:=&rerankingv1.RerankRequest{ExpectedRerankerProfileId:body.Profile,Query:body.Query}
    for _,item:=range body.Candidates{
        fields,e:=uniqueEvaluationObject(item)
        if e!=nil || len(fields)!=2 || fields["id"]==nil || fields["text"]==nil{return invalid()}
        var input struct{ID string `json:"id"`;Text string `json:"text"`}
        if json.Unmarshal(item,&input)!=nil{return invalid()}
        req.Candidates=append(req.Candidates,&rerankingv1.Candidate{Id:input.ID,Text:input.Text})
    }
    if err=reranking.ValidateRequest(req);err!=nil{return nil,err}
    return req,nil
}

func rerankingError(c fiber.Ctx,err error)error{
    s:=status.Convert(err)
    httpStatus:=map[codes.Code]int{codes.InvalidArgument:400,codes.NotFound:404,codes.FailedPrecondition:409,codes.Unavailable:503,codes.DataLoss:502,codes.DeadlineExceeded:504,codes.Canceled:408,codes.ResourceExhausted:429}[s.Code()]
    if httpStatus==0{httpStatus=500}
    reason,_,_:=strings.Cut(s.Message(),":")
    if !strings.HasPrefix(reason,"RERANK_"){reason="RERANK_"+strings.ToUpper(s.Code().String())}
    return c.Status(httpStatus).JSON(fiber.Map{"code":reason,"grpc_code":s.Code().String(),"error":s.Message()})
}

func rerankingJSON(c fiber.Ctx,value proto.Message)error{
    data,err:=(protojson.MarshalOptions{UseProtoNames:true,EmitUnpopulated:true}).Marshal(value)
    if err!=nil{return rerankingError(c,status.Error(codes.Internal,"reranker response encoding failed"))}
    c.Set("Content-Type","application/json")
    return c.Send(data)
}

func rerankingPath(c fiber.Ctx)(string,error){
    id,err:=uuid.Parse(c.Params("id"))
    if err!=nil || id==uuid.Nil || id.String()!=c.Params("id") || len(c.Request().URI().QueryString())!=0{return "",status.Error(codes.InvalidArgument,"RERANK_INVALID_REQUEST: canonical deployment UUID and no query parameters required")}
    return id.String(),nil
}

func(a *API)rerankerProfile(c fiber.Ctx)error{
    id,err:=rerankingPath(c);if err!=nil{return rerankingError(c,err)}
    ctx,cancel:=context.WithTimeout(c.Context(),25*time.Second);defer cancel()
    result,err:=a.clients.Reranking.GetProfile(ctx,&rerankingv1.GetProfileRequest{DeploymentId:id})
    if err!=nil{return rerankingError(c,err)}
    return rerankingJSON(c,result)
}

func(a *API)rerankDeployment(c fiber.Ctx)error{
    id,err:=rerankingPath(c);if err!=nil{return rerankingError(c,err)}
    req,err:=parseRerank(c.Body());if err!=nil{return rerankingError(c,err)}
    req.DeploymentId=id
    ctx,cancel:=context.WithTimeout(c.Context(),125*time.Second);defer cancel()
    result,err:=a.clients.Reranking.Rerank(ctx,req)
    if err!=nil{return rerankingError(c,err)}
    return rerankingJSON(c,result)
}
