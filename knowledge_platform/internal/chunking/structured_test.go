package chunking

import (
 "encoding/json"
 "fmt"
 "os"
 "path/filepath"
 "strings"
 "testing"
)

func checkedBuild(t *testing.T, spec Spec, raw map[string][]byte, source string) *Result {
 t.Helper();r,e:=Build(spec,raw);if e!=nil{t.Fatal(e)}
 encoded,_:=json.Marshal(r);if _,e=Verify(encoded,spec);e!=nil{t.Fatal(e)}
 again,e:=Build(spec,raw);if e!=nil||HashJSON(again)!=HashJSON(r){t.Fatal("non-deterministic split",e)}
 coverage:=make([]bool,len([]rune(source)))
 for _,c:=range r.Chunks {
  text:=[]rune(c.Text);if len(text)>spec.Chunker.Options.MaxCharacters{t.Fatal("context exceeded chunk budget")}
  for _,s:=range c.Spans {
   if string([]rune(source)[s.Start:s.End])!=string(text[s.ChunkStart:s.ChunkEnd]){t.Fatal("source span text differs")}
   for i:=s.Start;i<s.End;i++{coverage[i]=true}
  }
 }
 for _,covered:=range coverage{if !covered{t.Fatal("source character lost")}}
 if r.Documents[0].CoveredCharacters!=len(coverage){t.Fatal("repeated context inflated coverage")}
 return r
}

func TestMarkdownTableHeadersAndUnicode(t *testing.T) {
 header:="| 항목 | 설명 |\r\n| --- | --- |\r\n"
 source:=header+strings.Repeat("| 값😀 | 가\\|나 설명 |\r\n",12)
 for _,id:=range []string{"recursive_character_v1","structure_v1"} {
  t.Run(id,func(t *testing.T){
   spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":source}},id)
   r:=checkedBuild(t,spec,raw,source)
   if len(r.Chunks)<2{t.Fatal("table not split")}
   for i,c:=range r.Chunks {
    if !strings.HasPrefix(c.Text,header){t.Fatal("table header missing")}
    if c.Structure==nil||c.Structure.Part!=i+1||c.Structure.Parts!=len(r.Chunks){t.Fatal("fragment identity missing")}
    if i>0&&(len(c.Structure.ContextRows)!=1||c.Structure.ContextRows[0]!=0){t.Fatal("header context not identified")}
   }
  })
 }
}

func Test1352CharacterTableAtDefaultLimit(t *testing.T) {
 // Reproduce the reported size, not the unseen contents of the user's b90.
 source:="| 값 |\n| --- |\n"+strings.Repeat("| "+strings.Repeat("가",60)+" |\n",19)
 source+="| "+strings.Repeat("나",1352-len([]rune(source))-5)+" |\n"
 if len([]rune(source))!=1352{t.Fatal("wrong reproduction size")}
 for _,id:=range []string{"structure_v1","recursive_character_v1"} {
  spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":source}},id)
  spec.Chunker,_=Normalize(id,nil)
  r:=checkedBuild(t,spec,raw,source)
  if len(r.Chunks)<2{t.Fatal("oversized table was not split")}
 }
}

func TestCellRowsWithoutHeadersDoNotInventHeaders(t *testing.T) {
 source:="";var cells []any
 for row:=0;row<10;row++ {
  value:=fmt.Sprintf("행 %d %s",row,strings.Repeat("값",15))
  source+=value+"\n"
  cells=append(cells,map[string]any{"row":row,"column":0,"text":""},map[string]any{"row":row,"column":1,"text":value})
 }
 spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":source,"cells":cells}},"structure_v1")
 r:=checkedBuild(t,spec,raw,source)
 for _,c:=range r.Chunks{if len(c.Structure.ContextRows)!=0{t.Fatal("invented header context")}}
}

func TestOCRCellsAndMergedContext(t *testing.T) {
 source:="\n유형\n설명\n분류A\n"
 cells:=[]any{
  map[string]any{"row":0,"column":0,"text":"유형","header":true},
  map[string]any{"row":0,"column":1,"text":"설명","header":true},
  map[string]any{"row":1,"column":0,"text":"분류A","row_span":8,"column_span":1,"formula":"=A1"},
 }
 for i:=1;i<=8;i++ {v:=fmt.Sprintf("세부%d 한글😀 설명내용입니다",i);source+=v+"\n";cells=append(cells,map[string]any{"row":i,"column":1,"text":v})}
 spec,raw:=fixture(t,[]map[string]any{{"type":"table","text":source,"cells":cells,"location":map[string]any{"page":3,"bbox":[]int{1,2,3,4}},"html":"<table>original</table>"}},"structure_v1")
 r:=checkedBuild(t,spec,raw,source)
 if len(r.Chunks)<2{t.Fatal("table not split")}
 for i,c:=range r.Chunks {
  if !strings.Contains(c.Text,"분류A"){t.Fatal("merged parent lost")}
  if i>0&&len(c.Structure.ContextRows)!=2{t.Fatal("header and merged anchor must be marked")}
  var m map[string]any;_ =json.Unmarshal(c.Spans[0].Metadata,&m)
  if HashJSON(m["cells"])!=HashJSON(cells)||m["html"]!="<table>original</table>"{t.Fatal("original structure changed")}
 }
}

func TestCodeLinesPreserveIndentationAndNewlines(t *testing.T) {
 source:=strings.Repeat("\tprint(\"한글😀\")\r\n",16)+"return value"
 spec,raw:=fixture(t,[]map[string]any{{"type":"code","text":source}},"structure_v1")
 r:=checkedBuild(t,spec,raw,source)
 var joined strings.Builder
 for i,c:=range r.Chunks {
  joined.WriteString(c.Text)
  if c.Structure==nil||c.Structure.Unit!="lines"||len(c.Structure.ContextRows)!=0{t.Fatal("invalid code fragment")}
  if i<len(r.Chunks)-1&&!strings.HasSuffix(c.Text,"\n"){t.Fatal("code line cut")}
 }
 if joined.String()!=source{t.Fatal("code changed or overlap inserted")}
}

func TestAtomicRowsLinesAndUnmappedTextFailPrecisely(t *testing.T) {
 cases:=[]struct{name,kind,text,want string;cells []any}{
  {"code line","code",strings.Repeat("가",101),"CODE_LINE_TOO_LARGE",nil},
  {"table row","table","| 제목 |\n| --- |\n| "+strings.Repeat("가",101)+" |\n","TABLE_ROW_TOO_LARGE",nil},
  {"unmapped OCR","table",strings.Repeat("가",110),"TABLE_ROW_BOUNDARIES_UNAVAILABLE",[]any{map[string]any{"row":0,"column":0,"text":"different"}}},
  {"unknown grid","table",strings.Repeat("word\n",25),"TABLE_ROW_BOUNDARIES_UNAVAILABLE",nil},
 }
 for _,tc:=range cases {t.Run(tc.name,func(t *testing.T){
  spec,raw:=fixture(t,[]map[string]any{{"type":tc.kind,"text":tc.text,"cells":tc.cells}},"structure_v1")
  _,e:=Build(spec,raw);if e==nil||!strings.Contains(e.Error(),tc.want)||!strings.Contains(e.Error(),"max_characters=100"){t.Fatal(e)}
  if tc.want!="TABLE_ROW_BOUNDARIES_UNAVAILABLE"&&!strings.Contains(e.Error(),"required_characters="){t.Fatal("missing actionable size")}
 })}
}

func TestPreviousImplementationAndStoredResultRemainCompatible(t *testing.T) {
 spec,raw:=fixture(t,[]map[string]any{{"type":"code","text":"x = 1\n"}},"structure_v1")
 prior,e:=normalizeVersion(spec.Chunker.ID,mustOptions(spec.Chunker.Options),LegacyImplementation);if e!=nil{t.Fatal(e)}
 spec.Chunker=prior
 r:=checkedBuild(t,spec,raw,"x = 1\n")
 encoded,_:=json.Marshal(r)
 if strings.Contains(string(encoded),`"structure":`){t.Fatal("legacy result encoding changed")}
 c:=r.Chunks[0];oldID:=HashJSON(struct{Canonical,Policy,Content string;Spans []Span}{c.CanonicalID,prior.PolicyHash,c.ContentSHA256,c.Spans})
 if c.ID!=oldID{t.Fatal("legacy chunk identity changed")}
 large,input:=fixture(t,[]map[string]any{{"type":"code","text":strings.Repeat("x=1\n",30)}},"structure_v1")
 large.Chunker=prior
 if _,e=Build(large,input);e==nil||!strings.Contains(e.Error(),"STRUCTURED_BLOCK_TOO_LARGE"){t.Fatal("legacy retry silently changed policy",e)}
 if spec.Chunker.PolicyHash==rPolicy(t){t.Fatal("new policy identity unchanged")}
}
func rPolicy(t *testing.T)string{t.Helper();s,e:=Normalize("structure_v1",[]byte(`{"max_characters":100,"overlap_characters":10}`));if e!=nil{t.Fatal(e)};return s.PolicyHash}

func TestFragmentSequenceMustBeComplete(t *testing.T) {
 source:=strings.Repeat("print(1)\n",30)
 spec,raw:=fixture(t,[]map[string]any{{"type":"code","text":source}},"structure_v1")
 r:=checkedBuild(t,spec,raw,source)
 last:=&r.Chunks[len(r.Chunks)-1];last.Structure.Parts++;last.ID=chunkID(*last,spec.Chunker.PolicyHash)
 encoded,_:=json.Marshal(r);if _,e:=Verify(encoded,spec);e==nil{t.Fatal("incomplete fragment sequence accepted")}
}

func TestStoredFragmentMetadataCannotBeChanged(t *testing.T) {
 source:=strings.Repeat("print(1)\n",30)
 spec,raw:=fixture(t,[]map[string]any{{"type":"code","text":source}},"structure_v1")
 r:=checkedBuild(t,spec,raw,source)
 r.Chunks[0].Structure.End++
 encoded,_:=json.Marshal(r);if _,e:=Verify(encoded,spec);e==nil{t.Fatal("fragment metadata not bound to identity")}
}

// This fixture comes from a previously saved Data OCR result in the project.
// It is copied into both test targets; no OCR rerun.
func TestSavedOCRTableRows(t *testing.T) {
 path:=filepath.Join("testdata","ocr-table-left.json")
 raw,e:=os.ReadFile(path);if e!=nil{t.Fatal(e)}
 var b map[string]any;if e=json.Unmarshal(raw,&b);e!=nil{t.Fatal(e)}
 source:=b["text"].(string)
 spec,inputs:=fixture(t,[]map[string]any{b},"structure_v1")
 selection,e:=Normalize("structure_v1",[]byte(`{"max_characters":300,"overlap_characters":10}`));if e!=nil{t.Fatal(e)};spec.Chunker=selection
 r:=checkedBuild(t,spec,inputs,source)
 if len(r.Chunks)<2{t.Fatal("saved OCR table not split")}
}
