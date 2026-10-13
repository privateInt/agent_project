package indexing
import("math";"strings";"testing";"encoding/json";embeddingv1 "knowledge-platform/gen/model/embedding/v1";"knowledge-platform/internal/chunking";"google.golang.org/protobuf/proto")
func profile()*embeddingv1.EmbeddingProfile{return &embeddingv1.EmbeddingProfile{DeploymentId:"11111111-1111-4111-8111-111111111111",ModelVersionId:"22222222-2222-4222-8222-222222222222",ProfileId:"snowflake-ko-v1",EmbeddingSpaceId:"es_"+strings.Repeat("a",64),SourceManifestSha256:strings.Repeat("b",64),TokenizerSha256:strings.Repeat("c",64),RuntimeImageId:"sha256:"+strings.Repeat("d",64),RuntimeContainerId:strings.Repeat("e",64),ServedModelName:"deployment-11111111-1111-4111-8111-111111111111",RepoId:"dragonkue/snowflake-arctic-embed-l-v2.0-ko",QueryPrefix:"query: ",Pooling:"cls",Normalization:"l2",Dimension:1024,MaxInputTokens:2048,MaxBatchSize:16,MaxInputBytes:131072,Truncation:"reject",Capability:"embedding",Status:"ready"}}
func TestReceiptRejectsSpaceIdentityOrderAndInvalidVectors(t *testing.T){
 p:=profile();chunks:=[]chunking.Chunk{{ID:"c1"},{ID:"c2"}};v:=make([]float64,1024);v[0]=1
 valid:=&embeddingv1.EmbedResponse{Profile:p,Role:"document",Data:[]*embeddingv1.EmbeddingOutput{{Id:"c1",Index:0,Embedding:v,InputTokens:3},{Id:"c2",Index:1,Embedding:v,InputTokens:4}}}
 if e:=ValidateResult(p,chunks,valid);e!=nil{t.Fatal(e)}
 mutations:=[]func(*embeddingv1.EmbedResponse){func(r *embeddingv1.EmbedResponse){r.Profile.EmbeddingSpaceId="es_"+strings.Repeat("f",64)},func(r *embeddingv1.EmbedResponse){r.Profile.RuntimeContainerId=strings.Repeat("f",64)},func(r *embeddingv1.EmbedResponse){r.Role="query"},func(r *embeddingv1.EmbedResponse){r.Data=r.Data[:1]},func(r *embeddingv1.EmbedResponse){r.Data[0].Id="c2"},func(r *embeddingv1.EmbedResponse){r.Data[0].Index=1},func(r *embeddingv1.EmbedResponse){r.Data[0].Embedding=r.Data[0].Embedding[:1023]},func(r *embeddingv1.EmbedResponse){r.Data[0].Embedding[0]=math.NaN()},func(r *embeddingv1.EmbedResponse){r.Data[0].Embedding[0]=math.Inf(1)},func(r *embeddingv1.EmbedResponse){r.Data[0].Embedding[0]=0},func(r *embeddingv1.EmbedResponse){r.Data[0].InputTokens=2049}}
 for i,fn:=range mutations{r:=proto.Clone(valid).(*embeddingv1.EmbedResponse);fn(r);if ValidateResult(p,chunks,r)==nil{t.Fatalf("invalid receipt %d accepted",i)}}
}
func TestBatchBoundariesPreserveSourceAndRespectBytes(t *testing.T){
 p:=profile();p.MaxInputBytes=6;chunks:=[]chunking.Chunk{{ID:"a",Text:"한글"},{ID:"b",Text:"x"}}
 first,e:=Slice(chunks,0,p);if e!=nil||len(first)!=1{t.Fatal(first,e)};last,e:=Slice(chunks,1,p);if e!=nil||len(last)!=1||last[0].ID!="b"{t.Fatal(last,e)}
 p.MaxInputBytes=5;if _,e=Slice(chunks,0,p);e==nil{t.Fatal("oversized UTF8 input accepted")}
 result:=chunking.Result{Chunks:[]chunking.Chunk{{ID:"a",CanonicalID:"one"},{ID:"b",CanonicalID:"two"},{ID:"c",CanonicalID:"one"}}};selected:=Select(result,[]string{"one"});if len(selected)!=2||selected[0].ID!="a"||selected[1].ID!="c"{t.Fatal("subset changed order")}
 p=profile();large:=chunking.Chunk{ID:"a",Text:"x",Quality:json.RawMessage(`{"note":"`+strings.Repeat("a",1600<<10)+`"}`)};if _,e=Slice([]chunking.Chunk{large},0,p);e==nil{t.Fatal("oversized metadata accepted")}
}
