package listing

import (
    "encoding/base64"
    "encoding/json"
    "strings"
    "testing"
    "time"
    "google.golang.org/grpc/codes"
    "google.golang.org/grpc/status"
)

func TestCursorBoundaryAndFilterBinding(t *testing.T) {
    first,err:=New(1,"","models","Gemma","llm");if err!=nil{t.Fatal(err)}
    created:=time.Date(2026,9,9,8,0,0,123456000,time.UTC)
    token:=first.Next(created,"00000000-0000-0000-0000-000000000002")
    // Page size can change without changing the filter/collection identity.
    next,err:=New(100,token,"models","Gemma","llm");if err!=nil{t.Fatal(err)}
    q:=Query{};q.Equal("model_type","llm");sql:=q.Finish(next,"created_at","model_id")
    if !strings.Contains(sql,"(created_at,model_id) < ($2::timestamptz,$3::uuid)") || q.Args[1]!=created || q.Args[2]!="00000000-0000-0000-0000-000000000002" || q.Args[3]!=101 {t.Fatalf("lost boundary: %s %#v",sql,q.Args)}
    for _,tc:=range []struct{name,collection,q,kind string}{
        {"collection","model-versions","Gemma","llm"},{"query","models","Llama","llm"},{"type","models","Gemma","ocr"},
    } {t.Run(tc.name,func(t *testing.T){if _,err:=New(20,token,tc.collection,tc.q,tc.kind);status.Code(err)!=codes.InvalidArgument{t.Fatal("cursor accepted changed filters",err)}})}
}

func TestCursorRejectsInvalidInput(t *testing.T) {
    page,_:=New(20,"","models")
    good:=cursor{Version:1,Scope:page.scope,CreatedAt:time.Now().UTC(),ID:"00000000-0000-0000-0000-000000000001"}
    raw,_:=json.Marshal(good)
    tokens:=map[string]string{"base64":"!", "oversized":strings.Repeat("a",2049),"trailing-json":base64.RawURLEncoding.EncodeToString(append(raw,[]byte("{}")...)),"unknown-field":base64.RawURLEncoding.EncodeToString(append(raw[:len(raw)-1],[]byte(`,"unexpected":1}`)...))}
    for _,field:=range []string{"version","scope","time","id"}{
        bad:=good
        switch field {case "version":bad.Version=2;case "scope":bad.Scope="wrong";case "time":bad.CreatedAt=time.Time{};case "id":bad.ID="not-a-uuid"}
        data,_:=json.Marshal(bad);tokens[field]=base64.RawURLEncoding.EncodeToString(data)
    }
    for name,token:=range tokens {t.Run(name,func(t *testing.T){if _,err:=New(20,token,"models");status.Code(err)!=codes.InvalidArgument{t.Fatal(err)}})}
    for _,limit:=range []int32{-1,101} {if _,err:=New(limit,"","models");status.Code(err)!=codes.InvalidArgument{t.Fatal("bad limit accepted")}}
}

func TestLiteralSearchAndBoundedText(t *testing.T) {
    value:=`100%_\' OR true --`
    query:=Query{};query.Contains(value,"name","description")
    page,_:=New(20,"","models")
    sql:=query.Finish(page,"created_at","model_id")
    if strings.Contains(sql,value) || query.Args[0]!=`%100\%\_\\' OR true --%` {t.Fatalf("search not literal/parameterized: %s %#v",sql,query.Args)}
    if got,err:=Text("  한글 모델  ");err!=nil || got!="한글 모델" {t.Fatal(got,err)}
    if _,err:=Text(strings.Repeat("한",200));err!=nil{t.Fatal(err)}
    for _,value:=range []string{strings.Repeat("한",201),"x\x00y",string([]byte{255})} {if _,err:=Text(value);status.Code(err)!=codes.InvalidArgument{t.Fatal("bad query accepted")}}
}
