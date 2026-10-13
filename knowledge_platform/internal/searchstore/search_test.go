package searchstore

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"knowledge-platform/internal/indexing"
	"knowledge-platform/internal/retrieval"
	"knowledge-platform/internal/testsupport"
)

func TestSearchFiltersCandidatesAndRejectsUnconfirmedResults(t *testing.T) {
	v:=testsupport.SearchVersion();name,alias,_:=names(v.ID);vector:=make([]float64,1024);vector[0]=1
	q:=retrieval.Query{VersionID:v.ID,SpaceID:v.Profile.EmbeddingSpaceId,Vector:vector,TopK:1,CandidateK:2,Filters:retrieval.Filters{CanonicalIDs:[]string{v.CanonicalIDs[0]},Kinds:[]string{"text"}}}
	mode:="valid";posts:=0
	server:=httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter,r *http.Request){
		if r.Method=="GET"&&r.URL.Path=="/"+name {
			if mode=="missing"{w.WriteHeader(404);return}
			mapping:=Mapping(v);if mode=="foreign mapping"{mapping["_meta"]=map[string]any{"owner":"foreign"}}
			_ =json.NewEncoder(w).Encode(map[string]any{name:map[string]any{"mappings":mapping,"aliases":map[string]any{alias:map[string]any{}},"settings":map[string]any{"index":map[string]string{"knn":"true"}}}});return
		}
		posts++
		if r.Method!="POST"||r.URL.Path!="/"+name+"/_search"||r.URL.Query().Get("allow_partial_search_results")!="false"||r.URL.Query().Get("ignore_unavailable")!="false"{t.Error("unscoped or partial query")}
		raw,_:=io.ReadAll(r.Body);var body map[string]any;if json.Unmarshal(raw,&body)!=nil{t.Fatal("invalid query JSON")}
		if _,ok:=body["post_filter"];ok{t.Error("post-filter used")}
		knn:=body["query"].(map[string]any)["knn"].(map[string]any)["vector"].(map[string]any)
		if knn["k"]!=float64(2)||body["size"]!=float64(2){t.Error("candidate limit ignored")}
		filter:=knn["filter"].(map[string]any)["bool"].(map[string]any)["filter"].([]any)
		if len(filter)!=4{t.Error("missing identity or user filters")}
		for _,field:=range body["_source"].([]any){if field=="vector"{t.Error("vector requested in results")}}
		if mode=="malformed"{_,_=io.WriteString(w,`{"hits":`);return}
		if mode=="huge"{_,_=io.WriteString(w,strings.Repeat(" ",retrieval.MaxResponseBytes+1));return}
		chunk:=testsupport.SearchChunk(v,0)
		if mode=="outside filter"{chunk=testsupport.SearchChunk(v,1)}
		record:=indexing.Record{ID:chunk.ID,IndexVersionID:v.ID,SpaceID:q.SpaceID,SourceID:chunk.SourceDocumentID,CanonicalID:chunk.CanonicalID,Kind:chunk.Kind,Ordinal:chunk.Ordinal,ContentSHA:chunk.ContentSHA256,Provenance:chunk}
		if mode=="wrong space"{record.SpaceID="other"};if mode=="changed text"{record.Provenance.Text="changed"};if mode=="bad spans"{record.Provenance.Spans[0].ChunkEnd=999};if mode=="wrong source"{record.SourceID=v.Spec.Documents[1].SourceDocumentID}
		row:=map[string]any{"_index":name,"_id":chunk.ID,"_score":0.8,"_source":record}
		if mode=="wrong physical"{row["_index"]="another"};if mode=="null score"{row["_score"]=nil};if mode=="wrong score"{row["_score"]=2.0};if mode=="missing source"{delete(row,"_source")}
		rows:=[]any{row};if mode=="duplicate"{rows=append(rows,row)};if mode=="empty"{rows=[]any{}}
		shards:=map[string]any{"total":1,"successful":1,"failed":0};if mode=="shard failure"{shards["failed"]=1};if mode=="missing shards"{delete(shards,"total")}
		hits:=map[string]any{"hits":rows};if mode=="missing hits"{delete(hits,"hits")}
		out:=map[string]any{"took":1,"timed_out":false,"_shards":shards,"hits":hits}
		if mode=="timeout"{out["timed_out"]=true};if mode=="missing timeout"{delete(out,"timed_out")};if mode=="terminated"{out["terminated_early"]=true}
		_=json.NewEncoder(w).Encode(out)
	}));defer server.Close()
	c:=&Client{Base:server.URL,HTTP:server.Client()}
	good,err:=c.Search(context.Background(),v,q);if err!=nil||len(good.Hits)!=1||!strings.Contains(string(good.Hits[0].Quality),"needs_ocr"){t.Fatal(good,err)}
	for _,bad:=range []string{"missing","foreign mapping","malformed","huge","outside filter","wrong space","changed text","bad spans","wrong source","wrong physical","null score","wrong score","missing source","duplicate","shard failure","missing shards","missing hits","timeout","missing timeout","terminated"}{
		mode=bad;if _,err=c.Search(context.Background(),v,q);err==nil{t.Fatal("unconfirmed result accepted",mode)}
		if mode=="huge"&&!errors.Is(err,ErrResponseTooLarge){t.Fatal(err)}
	}
	mode="empty";empty,err:=c.Search(context.Background(),v,q);if err!=nil||len(empty.Hits)!=0{t.Fatal(empty,err)}
	if posts!=20{t.Fatal("unexpected search calls/retries",posts)}
}
