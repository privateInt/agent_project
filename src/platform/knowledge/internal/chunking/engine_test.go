package chunking
import("encoding/json";"fmt";"strings";"testing")

func fixture(t *testing.T,blocks []map[string]any,chunker string)(Spec,map[string][]byte){
 t.Helper()
 for i,b:=range blocks{b["id"]=stringID(i);b["order"]=i;if _,ok:=b["location"];!ok{b["location"]=map[string]any{}}}
 doc:=map[string]any{"schema_version":"canonical-document-v1","source":map[string]any{"document_id":"source-a","sha256":strings.Repeat("a",64),"format":"text"},"processor":map[string]any{"version":"cpu-parser-v1","rules_version":"basic-cleaning-v1","image_id":"sha256:"+strings.Repeat("b",64)},"blocks":blocks,"pages":[]any{},"quality":map[string]any{"state":"needs_review","issues":[]any{map[string]any{"code":"example_warning","location":map[string]any{"block_id":"b1"}}}}}
 raw,e:=json.Marshal(doc);if e!=nil{t.Fatal(e)}
 in:=Input{CanonicalID:"canonical-a",CanonicalSHA256:Digest(raw),SizeBytes:int64(len(raw)),SourceDocumentID:"source-a",SourceSHA256:strings.Repeat("a",64),RefinementRunID:"run-a",SchemaVersion:"canonical-document-v1",ProcessorVersion:"cpu-parser-v1",RulesVersion:"basic-cleaning-v1",ProcessorImageID:"sha256:"+strings.Repeat("b",64),Format:"text"}
 selected,e:=Normalize(chunker,[]byte(`{"max_characters":100,"overlap_characters":10}`));if e!=nil{t.Fatal(e)}
 spec,e:=NewSpec([]Input{in},selected);if e!=nil{t.Fatal(e)};return spec,map[string][]byte{in.CanonicalID:raw}
}
func stringID(i int)string{return fmt.Sprintf("b%d",i+1)}
func TestShortFactAndUnicodeSpans(t *testing.T){
 content:="입장료는 무료입니다. "+strings.Repeat("한글😀 경계 ",40)
 spec,raws:=fixture(t,[]map[string]any{{"type":"text","text":content}},"recursive_character_v1")
 result,e:=Build(spec,raws);if e!=nil{t.Fatal(e)}
 seen:=make([]bool,len([]rune(content)))
 for _,c:=range result.Chunks{for _,span:=range c.Spans{if string([]rune(content)[span.Start:span.End])!=string([]rune(c.Text)[span.ChunkStart:span.ChunkEnd]){t.Fatal("Unicode span differs from source")};for i:=span.Start;i<span.End;i++{seen[i]=true}}}
 for _,ok:=range seen{if !ok{t.Fatal("source rune disappeared")}}
 short,raw:=fixture(t,[]map[string]any{{"type":"paragraph","text":"입장료는 무료입니다."}},"structure_v1")
 r,e:=Build(short,raw);if e!=nil||len(r.Chunks)!=1||r.Chunks[0].Text!="입장료는 무료입니다."{t.Fatal("short fact was dropped",e)}
}
func TestStrategiesAndDeterminism(t *testing.T){
 blocks:=[]map[string]any{{"type":"heading","text":"안내"},{"type":"paragraph","text":"무료 입장"},{"type":"heading","text":"시간"},{"type":"paragraph","text":"오전 9시"}}
 a,raw:=fixture(t,blocks,"recursive_character_v1");r1,e:=Build(a,raw);if e!=nil{t.Fatal(e)}
 b,_:=fixture(t,blocks,"structure_v1");r2,e:=Build(b,raw);if e!=nil{t.Fatal(e)}
 if len(r1.Chunks)!=1||len(r2.Chunks)!=2{t.Fatal("strategies are aliases",len(r1.Chunks),len(r2.Chunks))}
 r3,e:=Build(b,raw);if e!=nil||HashJSON(r2)!=HashJSON(r3){t.Fatal("nondeterministic result",e)}
 encoded,_:=json.Marshal(r2);if _,e=Verify(encoded,b);e!=nil{t.Fatal(e)}
}
func TestStructurePayloadAndExclusions(t *testing.T){
 cells:=[]any{map[string]any{"row":0,"column":0,"row_span":2,"column_span":1,"text":"무료","formula":"=A1"}}
 spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":"| 요금 |\n| 무료 |","cells":cells,"location":map[string]any{"sheet":"요금표","range":"A1:B2"}},{"type":"code","text":"x = 1\n  y = 2"},{"type":"image","text":"그림 영역: 원본 자료에서 확인","recognition_status":"unread"}},"structure_v1")
 r,e:=Build(spec,raw);if e!=nil{t.Fatal(e)}
 if len(r.Chunks)!=2||len(r.Exclusions)!=1||r.Exclusions[0].BlockID!="b3"{t.Fatal("placeholder indexed or content lost")}
 var metadata map[string]any;_ =json.Unmarshal(r.Chunks[0].Spans[0].Metadata,&metadata)
 if HashJSON(metadata["cells"])!=HashJSON(cells)||metadata["location"].(map[string]any)["sheet"]!="요금표"{t.Fatal("table details lost")}
 if _,exists:=metadata["location"].(map[string]any)["page"];exists{t.Fatal("invented page")}
 if r.Chunks[1].Text!="x = 1\n  y = 2"{t.Fatal("code normalized")}
}
func TestRejectOversizedStructureAndMissingInput(t *testing.T){
 spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":strings.Repeat("값",101)}},"structure_v1")
 if _,e:=Build(spec,raw);e==nil||!strings.Contains(e.Error(),"TABLE_ROW_BOUNDARIES_UNAVAILABLE"){t.Fatal("oversized table silently cut",e)}
 if _,e:=Build(spec,map[string][]byte{});e==nil{t.Fatal("missing canonical accepted")}
}
func TestRecognizedFigureTextAndSeparatorOnlyWindow(t *testing.T){
 spec,raw:=fixture(t,[]map[string]any{{"type":"image","text":"비상구는 오른쪽입니다."},{"type":"unhandled_structure","text":"x² + y² = 1"}},"structure_v1")
 r,e:=Build(spec,raw);if e!=nil||len(r.Exclusions)!=0||!strings.Contains(r.Chunks[0].Text,"비상구"){t.Fatal("recognized figure text lost",e)}
 spec,raw=fixture(t,[]map[string]any{{"type":"text","text":strings.Repeat("가",100)},{"type":"text","text":strings.Repeat("나",100)}},"recursive_character_v1")
 selected,e:=Normalize("recursive_character_v1",[]byte(`{"max_characters":100,"overlap_characters":0}`));if e!=nil{t.Fatal(e)};spec.Chunker=selected
 if _,e=Build(spec,raw);e!=nil{t.Fatal("synthetic separator made an empty source chunk",e)}
}
func TestIdentityRejectsMutationAndDuplicateInput(t *testing.T){
 spec,raw:=fixture(t,[]map[string]any{{"type":"text","text":"same name"}},"structure_v1")
 r,e:=Build(spec,raw);if e!=nil{t.Fatal(e)}
 changed:=spec;changed.Documents=append([]Input(nil),spec.Documents...);changed.Documents[0].CanonicalID="canonical-b";changed.SnapshotID=HashJSON(changed.Documents)
 other,e:=Build(changed,map[string][]byte{"canonical-b":raw["canonical-a"]});if e!=nil{t.Fatal(e)}
 if r.Chunks[0].ID==other.Chunks[0].ID{t.Fatal("canonical identities collided")}
 tampered:=append([]byte(nil),raw["canonical-a"]...);tampered[len(tampered)-2]^=1
 if _,e:=Build(spec,map[string][]byte{"canonical-a":tampered});e==nil{t.Fatal("hash mismatch accepted")}
 if _,e:=NewSpec([]Input{spec.Documents[0],spec.Documents[0]},spec.Chunker);e==nil{t.Fatal("duplicate input accepted")}
 changed=spec;changed.Chunker.Version="newer";if _,e:=Build(changed,raw);e==nil{t.Fatal("retry switched implementation")}
}
func TestOptionsAndDuplicateJSON(t *testing.T){
 for _,raw:=range []string{`{"max_characters":100,"overlap_characters":50}`,`{"overlap_characters":-1}`,`{"max_characters":null}`,`{"unknown":1}`,`{"max_characters":100,"max_characters":200}`,`[]`,`null`}{if _,e:=Normalize("structure_v1",[]byte(raw));e==nil{t.Fatalf("invalid options accepted: %s",raw)}}
 if _,e:=Normalize("arbitrary_plugin",nil);e==nil{t.Fatal("unknown plugin accepted")}
 var decoded map[string]any;if Decode([]byte(`{"source":{"id":1,"id":2}}`),&decoded)==nil{t.Fatal("nested duplicate accepted")}
}
