package app

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	indexingv1 "knowledge-platform/gen/indexing/v1"
	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type readBackend struct {
	*indexBackend
	searchFn func(context.Context,indexing.Version,retrieval.Query)(retrieval.Candidates,error)
}
func (b *readBackend) Search(ctx context.Context,v indexing.Version,q retrieval.Query)(retrieval.Candidates,error){return b.searchFn(ctx,v,q)}
func searchRequest(v indexing.Version)*indexingv1.SearchRequest {
	vector:=make([]float64,1024);vector[0]=1
	raw,_:=json.Marshal(map[string]any{"index_version_id":v.ID,"embedding_space_id":v.Profile.EmbeddingSpaceId,"query_embedding":vector})
	return &indexingv1.SearchRequest{IndexId:v.IndexID,QueryJson:raw}
}
func markSearchReady(t *testing.T,s *IndexService,v indexing.Version){
	t.Helper();_,err:=s.chunks.pool.Exec(context.Background(),`UPDATE index_versions SET state='ready',reference_state='held',chunk_count=1,indexed_count=1 WHERE index_version_id=$1`,v.ID);if err!=nil{t.Fatal(err)}
}
func TestSearchPinsOldVersionAndNeverCallsModel(t *testing.T){
	s,m,_,_,backend,_,runID:=indexFixture(t);ctx:=context.Background();idx,old,_:=newVersion(t,s,m,runID);markSearchReady(t,s,old)
	raw,err:=s.CreateBuild(ctx,&indexingv1.CreateBuildRequest{IndexId:idx.ID,ChunkingRunId:runID,IdempotencyKey:"new-search-version"});if err!=nil{t.Fatal(err)};newer,_:=versionFrom(raw.Json);markSearchReady(t,s,newer)
	current,_:=s.GetIndex(ctx,&indexingv1.IndexRequest{IndexId:idx.ID});_=json.Unmarshal(current.Json,&idx)
	if _,err=s.Activate(ctx,&indexingv1.MutationRequest{IndexId:idx.ID,IndexVersionId:newer.ID,ExpectedRevision:idx.Revision});err!=nil{t.Fatal(err)}
	modelCalls:=m.calls;m.offline=true;calls:=0
	s.search=&readBackend{indexBackend:backend,searchFn:func(_ context.Context,v indexing.Version,_ retrieval.Query)(retrieval.Candidates,error){calls++;if v.ID!=old.ID{t.Error("active version silently substituted")};return retrieval.Candidates{Hits:[]retrieval.Hit{}},nil}}
	result,err:=s.Search(ctx,searchRequest(old));if err!=nil{t.Fatal(err)}
	var got retrieval.Response;if json.Unmarshal(result.Json,&got)!=nil||got.VersionID!=old.ID||got.EmptyReason!="no_matches"||m.calls!=modelCalls||calls!=1{t.Fatal("search crossed model/version boundary",got)}
	bad:=searchRequest(old);bad.QueryJson=[]byte(strings.Replace(string(bad.QueryJson),old.Profile.EmbeddingSpaceId,"es_"+strings.Repeat("f",64),1))
	if _,err=s.Search(ctx,bad);status.Code(err)!=codes.FailedPrecondition||calls!=1{t.Fatal("space mismatch reached backend",err)}
	if _,err=s.DeleteVersion(ctx,&indexingv1.VersionRequest{IndexVersionId:old.ID});err!=nil{t.Fatal(err)}
	if _,err=s.Search(ctx,searchRequest(old));status.Code(err)!=codes.FailedPrecondition||calls!=1{t.Fatal("deleting version searched",err)}
	otherRaw,err:=s.CreateIndex(ctx,&indexingv1.CreateIndexRequest{Name:"other",EmbeddingDeploymentId:m.p.DeploymentId,IdempotencyKey:"other-search-index"});if err!=nil{t.Fatal(err)}
	var other indexing.Index;_=json.Unmarshal(otherRaw.Json,&other)
	wrongParent:=searchRequest(newer);wrongParent.IndexId=other.ID
	if _,err=s.Search(ctx,wrongParent);status.Code(err)!=codes.NotFound||calls!=1{t.Fatal("foreign version searched",err)}
}

func TestDeleteWaitsForBoundedInFlightSearch(t *testing.T){
	s,m,_,_,backend,_,runID:=indexFixture(t);ctx:=context.Background();idx,v,_:=newVersion(t,s,m,runID);markSearchReady(t,s,v)
	current,_:=s.GetIndex(ctx,&indexingv1.IndexRequest{IndexId:idx.ID});_=json.Unmarshal(current.Json,&idx)
	entered,release:=make(chan struct{}),make(chan struct{})
	s.search=&readBackend{indexBackend:backend,searchFn:func(ctx context.Context,_ indexing.Version,_ retrieval.Query)(retrieval.Candidates,error){close(entered);select{case<-release:return retrieval.Candidates{},nil;case<-ctx.Done():return retrieval.Candidates{},ctx.Err()}}}
	searchDone:=make(chan error,1);go func(){_,err:=s.Search(ctx,searchRequest(v));searchDone<-err}()
	select{case<-entered:case<-time.After(5*time.Second):t.Fatal("search did not start")}
	deleteCtx,cancel:=context.WithTimeout(ctx,100*time.Millisecond)
	_,err:=s.DeleteIndex(deleteCtx,&indexingv1.MutationRequest{IndexId:idx.ID,ExpectedRevision:idx.Revision});cancel()
	if err==nil{close(release);t.Fatal("deletion bypassed in-flight read lock")}
	close(release);if err=<-searchDone;err!=nil{t.Fatal(err)}
	if _,err=s.DeleteIndex(ctx,&indexingv1.MutationRequest{IndexId:idx.ID,ExpectedRevision:idx.Revision});err!=nil{t.Fatal("delete did not proceed after search",err)}
	if _,err=s.Search(ctx,searchRequest(v));status.Code(err)!=codes.FailedPrecondition{t.Fatal("deleted parent accepted a search",err)}
}

func TestSearchCancellationReleasesLocks(t *testing.T){
	s,m,_,_,backend,_,runID:=indexFixture(t);ctx:=context.Background();idx,v,_:=newVersion(t,s,m,runID);markSearchReady(t,s,v)
	s.search=&readBackend{indexBackend:backend,searchFn:func(ctx context.Context,_ indexing.Version,_ retrieval.Query)(retrieval.Candidates,error){<-ctx.Done();return retrieval.Candidates{},ctx.Err()}}
	deadline,cancel:=context.WithTimeout(ctx,100*time.Millisecond);defer cancel()
	if _,err:=s.Search(deadline,searchRequest(v));status.Code(err)!=codes.DeadlineExceeded{t.Fatal("deadline lost",err)}
	current,err:=s.GetIndex(ctx,&indexingv1.IndexRequest{IndexId:idx.ID});if err!=nil{t.Fatal(err)};_=json.Unmarshal(current.Json,&idx)
	cleanup,stop:=context.WithTimeout(ctx,5*time.Second);defer stop()
	if _,err=s.DeleteIndex(cleanup,&indexingv1.MutationRequest{IndexId:idx.ID,ExpectedRevision:idx.Revision});err!=nil{t.Fatal("search lock leaked",err)}
}
