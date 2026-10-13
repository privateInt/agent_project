package app

import (
    "context"
    "encoding/json"
    "strings"
    "time"
    "unicode/utf8"

    embeddingv1 "model-platform/gen/embedding/v1"
    "model-platform/internal/embedding"
    "github.com/gofiber/fiber/v3"
    "github.com/google/uuid"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
    "google.golang.org/protobuf/encoding/protojson"
    "google.golang.org/protobuf/proto"
)

func parseEmbedding(raw []byte)(*embeddingv1.EmbedRequest,error){
    invalid:=func()(*embeddingv1.EmbedRequest,error){return nil,status.Error(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST: expected space, role and id/text inputs are required; no other fields allowed")}
    if len(raw)>1<<20 || !utf8.Valid(raw){return invalid()}
    object,err:=uniqueEvaluationObject(raw)
    if err!=nil || len(object)!=3 || object["expected_embedding_space_id"]==nil || object["role"]==nil || object["inputs"]==nil{return invalid()}
    var body struct{Space string `json:"expected_embedding_space_id"`;Role string `json:"role"`;Inputs []json.RawMessage `json:"inputs"`}
    if json.Unmarshal(raw,&body)!=nil{return invalid()}
    req:=&embeddingv1.EmbedRequest{ExpectedEmbeddingSpaceId:body.Space,Role:body.Role}
    for _,item:=range body.Inputs{
        fields,e:=uniqueEvaluationObject(item)
        if e!=nil || len(fields)!=2 || fields["id"]==nil || fields["text"]==nil{return invalid()}
        var input struct{ID string `json:"id"`;Text string `json:"text"`}
        if json.Unmarshal(item,&input)!=nil{return invalid()}
        req.Inputs=append(req.Inputs,&embeddingv1.EmbeddingInput{Id:input.ID,Text:input.Text})
    }
    if err=embedding.ValidateRequest(req);err!=nil{return nil,err}
    return req,nil
}

func embeddingError(c fiber.Ctx,err error)error{
    s:=status.Convert(err)
    httpStatus:=map[codes.Code]int{codes.InvalidArgument:400,codes.NotFound:404,codes.FailedPrecondition:409,codes.Unavailable:503,codes.DataLoss:502,codes.DeadlineExceeded:504,codes.Canceled:408,codes.ResourceExhausted:429}[s.Code()]
    if httpStatus==0{httpStatus=500}
    reason,_,_:=strings.Cut(s.Message(),":")
    if !strings.HasPrefix(reason,"EMBEDDING_"){reason="EMBEDDING_"+strings.ToUpper(s.Code().String())}
    return c.Status(httpStatus).JSON(fiber.Map{"code":reason,"grpc_code":s.Code().String(),"error":s.Message()})
}

func embeddingJSON(c fiber.Ctx,value proto.Message)error{
    data,err:=(protojson.MarshalOptions{UseProtoNames:true,EmitUnpopulated:true}).Marshal(value)
    if err!=nil{return embeddingError(c,status.Error(codes.Internal,"embedding response encoding failed"))}
    c.Set("Content-Type","application/json")
    return c.Send(data)
}

func embeddingPath(c fiber.Ctx)(string,error){
    id,err:=uuid.Parse(c.Params("id"))
    if err!=nil || id==uuid.Nil || id.String()!=c.Params("id") || len(c.Request().URI().QueryString())!=0{return "",status.Error(codes.InvalidArgument,"EMBEDDING_INVALID_REQUEST: canonical deployment UUID and no query parameters required")}
    return id.String(),nil
}

func(a *API)embeddingProfile(c fiber.Ctx)error{
    id,err:=embeddingPath(c);if err!=nil{return embeddingError(c,err)}
    ctx,cancel:=context.WithTimeout(c.Context(),25*time.Second);defer cancel()
    result,err:=a.clients.Embedding.GetProfile(ctx,&embeddingv1.GetProfileRequest{DeploymentId:id})
    if err!=nil{return embeddingError(c,err)}
    return embeddingJSON(c,result)
}

func(a *API)embedDeployment(c fiber.Ctx)error{
    id,err:=embeddingPath(c);if err!=nil{return embeddingError(c,err)}
    req,err:=parseEmbedding(c.Body());if err!=nil{return embeddingError(c,err)}
    req.DeploymentId=id
    ctx,cancel:=context.WithTimeout(c.Context(),125*time.Second);defer cancel()
    result,err:=a.clients.Embedding.Embed(ctx,req)
    if err!=nil{return embeddingError(c,err)}
    return embeddingJSON(c,result)
}
