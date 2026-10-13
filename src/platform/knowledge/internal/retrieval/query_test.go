package retrieval

import (
	"encoding/json"
	"strings"
	"testing"

	"knowledge-platform/internal/testsupport"
)

func queryBody() map[string]any {
	v := testsupport.SearchVersion()
	vector := make([]float64, 1024); vector[0] = 1
	return map[string]any{"index_version_id": v.ID, "embedding_space_id": v.Profile.EmbeddingSpaceId, "query_embedding": vector}
}
func parsedQuery(t *testing.T) Query {
	t.Helper(); raw, _ := json.Marshal(queryBody()); q, err := Parse(raw); if err != nil { t.Fatal(err) }; return q
}
func TestStrictSearchRequest(t *testing.T) {
	q := parsedQuery(t)
	if q.TopK != 5 || q.CandidateK != 5 { t.Fatal("defaults", q.TopK, q.CandidateK) }
	cases := map[string]func(map[string]any){
		"missing version": func(b map[string]any){delete(b,"index_version_id")},
		"invalid UUID": func(b map[string]any){b["index_version_id"]="active"},
		"wrong dimension": func(b map[string]any){b["query_embedding"]=[]float64{1}},
		"zero": func(b map[string]any){b["query_embedding"]=make([]float64,1024)},
		"not unit": func(b map[string]any){b["query_embedding"].([]float64)[0]=2},
		"null element": func(b map[string]any){a:=make([]any,1024);for i:=range a{a[i]=0};a[0]=1;a[5]=nil;b["query_embedding"]=a},
		"string element": func(b map[string]any){a:=make([]any,1024);for i:=range a{a[i]=0};a[0]="1";b["query_embedding"]=a},
		"raw question": func(b map[string]any){b["query_text"]="do not infer"},
		"zero top": func(b map[string]any){b["top_k"]=0},
		"float top": func(b map[string]any){b["top_k"]=1.5},
		"null top": func(b map[string]any){b["top_k"]=nil},
		"candidate too small": func(b map[string]any){b["candidate_k"]=4},
		"candidate too large": func(b map[string]any){b["candidate_k"]=101},
		"null filters": func(b map[string]any){b["filters"]=nil},
		"unknown filter": func(b map[string]any){b["filters"]=map[string]any{"group_id":"all"}},
		"empty IDs": func(b map[string]any){b["filters"]=map[string]any{"canonical_ids":[]string{}}},
		"null IDs": func(b map[string]any){b["filters"]=map[string]any{"canonical_ids":nil}},
		"duplicate IDs": func(b map[string]any){id:=testsupport.SearchVersion().CanonicalIDs[0];b["filters"]=map[string]any{"canonical_ids":[]string{id,id}}},
		"unsupported kind": func(b map[string]any){b["filters"]=map[string]any{"kinds":[]string{"image"}}},
		"rerank enabled": func(b map[string]any){b["rerank"]=map[string]any{"enabled":true}},
		"rerank null": func(b map[string]any){b["rerank"]=map[string]any{"enabled":nil}},
		"rerank unknown": func(b map[string]any){b["rerank"]=map[string]any{"enabled":false,"deployment_id":"unused"}},
		"case alias": func(b map[string]any){b["TOP_K"]=1},
	}
	for name, change := range cases { t.Run(name,func(t *testing.T){b:=queryBody();change(b);raw,err:=json.Marshal(b);if err!=nil{t.Fatal(err)};if _,err=Parse(raw);err==nil{t.Fatal("invalid query accepted")}}) }
	raw,_:=json.Marshal(queryBody())
	for _, bad := range [][]byte{append(append([]byte{},raw...),[]byte(`{}`)...), []byte(`null`), []byte(`[]`), []byte(strings.Repeat(" ",MaxRequestBytes+1)), []byte(strings.Replace(string(raw),`"index_version_id":`,`"top_k":2,"top_k":3,"index_version_id":`,1))} {
		if _,err:=Parse(bad);err==nil{t.Fatal("malformed JSON accepted")}
	}
}
func TestVersionSpaceAndDocumentIntersection(t *testing.T) {
	v:=testsupport.SearchVersion();q:=parsedQuery(t)
	if err:=Validate(v,q);err!=nil{t.Fatal(err)}
	wrong:=q;wrong.SpaceID="es_"+strings.Repeat("f",64)
	if err:=Validate(v,wrong);err==nil||!strings.HasPrefix(err.Error(),"SEARCH_SPACE_MISMATCH:"){t.Fatal("equal dimension crossed spaces",err)}
	q.Filters=Filters{CanonicalIDs:[]string{v.CanonicalIDs[0]},SourceIDs:[]string{v.Spec.Documents[1].SourceDocumentID}}
	if _,err:=Scope(v,q);err==nil||!strings.HasPrefix(err.Error(),"SEARCH_EMPTY_SCOPE:"){t.Fatal("empty intersection broadened",err)}
	q.Filters=Filters{CanonicalIDs:[]string{v.CanonicalIDs[1]}}
	v.CanonicalIDs=v.CanonicalIDs[:1]
	if _,err:=Scope(v,q);err==nil{t.Fatal("excluded canonical re-entered scope")}
	q.Filters=Filters{CanonicalIDs:[]string{v.CanonicalIDs[0]},Kinds:[]string{"table"}}
	scope,err:=Scope(v,q);if err!=nil||len(scope)!=1{t.Fatal(err)}
	if MatchChunk(testsupport.SearchChunk(v,0),scope,q){t.Fatal("kind filter ignored")}
}
func TestRanksAndEmptyResultsKeepScoreMeaning(t *testing.T) {
	v:=testsupport.SearchVersion();q:=parsedQuery(t);q.TopK=1;q.CandidateK=2
	a,b:=testsupport.SearchChunk(v,0),testsupport.SearchChunk(v,1)
	out,err:=Finish(v.IndexID,q,Candidates{Hits:[]Hit{{Chunk:a,Score:0.8},{Chunk:b,Score:0.8}}},Timings{})
	if err!=nil||out.CandidateCount!=2||out.ReturnedCount!=1||out.Results[0].Rank!=1||out.Results[0].RetrievalRank!=1||out.Rerank.Applied||out.Scores.IsProbability{t.Fatal(out,err)}
	want:=a.ID;if b.ID<want{want=b.ID};if out.Results[0].ID!=want{t.Fatal("unstable tie")}
	empty,err:=Finish(v.IndexID,q,Candidates{},Timings{});if err!=nil||empty.EmptyReason!="no_matches"||empty.Results==nil||len(empty.Results)!=0{t.Fatal(empty,err)}
}
