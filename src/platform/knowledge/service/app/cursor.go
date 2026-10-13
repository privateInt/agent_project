package app
import("encoding/base64";"encoding/json";"knowledge-platform/internal/chunking";"google.golang.org/grpc/codes";"google.golang.org/grpc/status")
func encodeCursor(c chunkCursor)string{b,_:=json.Marshal(c);return base64.RawURLEncoding.EncodeToString(b)}
func decodeCursor(raw,run,hash string,count int)(int,error){
 if raw==""{return 0,nil};bad:=status.Error(codes.InvalidArgument,"invalid or unrelated chunk cursor")
 if len(raw)>1024{return 0,bad};b,e:=base64.RawURLEncoding.DecodeString(raw);if e!=nil{return 0,bad};var c chunkCursor
 if chunking.Decode(b,&c)!=nil||c.Version!=1||c.Run!=run||c.Hash!=hash||c.Offset<1||c.Offset>=count{return 0,bad};return c.Offset,nil
}
