package chunking

import (
 "encoding/json"
 "fmt"
 "sort"
 "strings"
 "unicode/utf8"
)

// Structure describes an exact slice of a table or code block. Row/line numbers
// are zero-based, end-exclusive. ContextRows are repeated source rows, not new
// facts or part of the fragment's primary range.
type Structure struct {
 Unit string `json:"unit"`
 Start int `json:"start"`
 End int `json:"end"`
 Part int `json:"part"`
 Parts int `json:"parts"`
 ContextRows []int `json:"context_rows,omitempty"`
}
type sourceUnit struct { start, end, row int }
type tableCell struct {
 Row int `json:"row"`
 Column int `json:"column"`
 RowSpan int `json:"row_span"`
 ColumnSpan int `json:"column_span"`
 Text string `json:"text"`
 Header bool `json:"header"`
}
type layout struct { units []sourceUnit; headers int; cells []tableCell }
type fragment struct { ranges [][2]int; info Structure }

func lines(text []rune) []sourceUnit {
 var out []sourceUnit
 start:=0
 for i,r:=range text {if r=='\n' {out=append(out,sourceUnit{start,i+1,len(out)});start=i+1}}
 if start<len(text) {out=append(out,sourceUnit{start,len(text),len(out)})}
 return out
}

// Accept only an explicit pipe-table header/separator. Arbitrary OCR newlines
// are not row boundaries. Escaped pipes are cell content.
func pipeCells(s string) []string {
 s=strings.TrimSpace(s)
 if !strings.HasPrefix(s,"|") || !strings.HasSuffix(s,"|") {return nil}
 var out []string;start:=1;escaped:=false
 for i:=1;i<len(s);i++ {
  if escaped {escaped=false;continue}
  if s[i]=='\\' {escaped=true;continue}
  if s[i]=='|' {out=append(out,strings.TrimSpace(s[start:i]));start=i+1}
 }
 if start!=len(s) {return nil}
 return out
}
func markdownRows(text []rune) (layout,bool) {
 ls:=lines(text);if len(ls)<3{return layout{},false}
 head:=pipeCells(string(text[ls[0].start:ls[0].end]));sep:=pipeCells(string(text[ls[1].start:ls[1].end]))
 if len(head)==0 || len(sep)!=len(head){return layout{},false}
 for _,v:=range sep {v=strings.Trim(v,":");if len(v)<3 || strings.Trim(v,"-")!=""{return layout{},false}}
 out:=layout{headers:1,units:[]sourceUnit{{0,ls[1].end,0}}}
 for _,line:=range ls[2:] {
  if len(pipeCells(string(text[line.start:line.end])))!=len(head){return layout{},false}
  out.units=append(out.units,sourceUnit{line.start,line.end,len(out.units)})
 }
 return out,true
}

func tableLayout(b block) (layout,error) {
 var meta struct {Cells []tableCell `json:"cells"`}
 if json.Unmarshal(b.Metadata,&meta)!=nil {return layout{},fmt.Errorf("invalid table metadata")}
 if len(meta.Cells)>20000 {return layout{},fmt.Errorf("table cell limit exceeded")}
 cells:=meta.Cells
 sort.SliceStable(cells,func(i,j int)bool{if cells[i].Row==cells[j].Row{return cells[i].Column<cells[j].Column};return cells[i].Row<cells[j].Row})
 for i:=range cells {
  c:=&cells[i]
  if c.RowSpan==0{c.RowSpan=1};if c.ColumnSpan==0{c.ColumnSpan=1}
  if c.Row<0||c.Row>100000||c.Column<0||c.Column>100000||c.RowSpan<1||c.RowSpan>100000||c.ColumnSpan<1||c.ColumnSpan>100000{return layout{},fmt.Errorf("invalid table coordinates")}
  if i>0&&cells[i-1].Row==c.Row&&cells[i-1].Column==c.Column{return layout{},fmt.Errorf("duplicate table cell")}
 }
 if l,ok:=markdownRows(b.Text);ok {
  // A merged grid cannot be assumed to match Markdown's one-row-per-line
  // representation without mapping the cell contents.
  for _,c:=range cells {if c.RowSpan>1||c.ColumnSpan>1{return layout{},fmt.Errorf("merged Markdown table needs an explicit source mapping")}}
  l.cells=cells;return l,nil
 }
 if len(cells)==0{return layout{},fmt.Errorf("table has no mapped cells or explicit Markdown rows")}
 source:=string(b.Text);offset:=0;runeOffset:=0
 l:=layout{cells:cells};rowHeader:=map[int]bool{};rowSeen:=map[int]bool{}
 for _,c:=range cells {
  if !rowSeen[c.Row] {rowHeader[c.Row]=true;rowSeen[c.Row]=true}
  rowHeader[c.Row]=rowHeader[c.Row]&&c.Header
  value:=strings.TrimSpace(c.Text);if value==""{continue}
  index:=strings.Index(source[offset:],value)
  if index<0||strings.TrimSpace(source[offset:offset+index])!="" {return layout{},fmt.Errorf("cell text cannot be aligned to source text")}
  start:=runeOffset+utf8.RuneCountInString(source[offset:offset+index])
  if len(l.units)==0||l.units[len(l.units)-1].row!=c.Row {
   if len(l.units)==0{start=0}else{l.units[len(l.units)-1].end=start}
   l.units=append(l.units,sourceUnit{start,0,c.Row})
  }
  consumed:=index+len(value);runeOffset+=utf8.RuneCountInString(source[offset:offset+consumed]);offset+=consumed
 }
 if len(l.units)==0||strings.TrimSpace(source[offset:])!=""{return layout{},fmt.Errorf("unmapped table text")}
 l.units[len(l.units)-1].end=len(b.Text)
 for _,u:=range l.units {if !rowHeader[u.row]{break};l.headers++}
 return l,nil
}

func structureFragments(b block,maximum int) ([]fragment,error) {
 l:=layout{units:lines(b.Text)};unit:="lines"
 if b.Kind=="table" {
  var e error;l,e=tableLayout(b);if e!=nil{return nil,fmt.Errorf("TABLE_ROW_BOUNDARIES_UNAVAILABLE: block=%s characters=%d max_characters=%d; %s",b.ID,len(b.Text),maximum,e)}
  unit="rows"
 }
 var out []fragment
 for start:=0;start<len(l.units); {
  contexts:=map[int]bool{}
  for i:=0;i<min(start,l.headers);i++{contexts[i]=true}
  // Carry the original anchor row when a vertically merged cell continues in
  // this fragment. Preserve the complete cells/HTML in every source span.
  for _,c:=range l.cells {
   if c.Row<l.units[start].row&&c.Row+c.RowSpan>l.units[start].row&&strings.TrimSpace(c.Text)!="" {
    for i:=0;i<start;i++{if l.units[i].row==c.Row{contexts[i]=true;break}}
   }
  }
  f:=fragment{info:Structure{Unit:unit,Start:l.units[start].row,Part:len(out)+1}}
  prefix:=0
  for i:=0;i<start;i++ {if contexts[i]{u:=l.units[i];f.ranges=append(f.ranges,[2]int{u.start,u.end});prefix+=u.end-u.start;f.info.ContextRows=append(f.info.ContextRows,u.row)}}
  minimumEnd:=start+1
  if start==0&&l.headers>0 {minimumEnd=min(len(l.units),l.headers+1)}
  required:=prefix+l.units[minimumEnd-1].end-l.units[start].start
  if required>maximum {
   code:="TABLE_ROW_TOO_LARGE";if b.Kind=="code"{code="CODE_LINE_TOO_LARGE"}
   return nil,fmt.Errorf("%s: block=%s %s=%d required_characters=%d max_characters=%d (including repeated context)",code,b.ID,unit,l.units[minimumEnd-1].row+1,required,maximum)
  }
  end:=minimumEnd
  for end<len(l.units)&&prefix+l.units[end].end-l.units[start].start<=maximum{end++}
  f.ranges=append(f.ranges,[2]int{l.units[start].start,l.units[end-1].end})
  f.info.End=l.units[end-1].row+1
  out=append(out,f);if len(out)>MaxChunks{return nil,fmt.Errorf("chunk count exceeds 20000")}
  start=end
 }
 for i:=range out{out[i].info.Parts=len(out)}
 return out,nil
}

func chunkID(c Chunk,policy string) string {
 if c.Structure==nil{return HashJSON(struct{Canonical,Policy,Content string;Spans []Span}{c.CanonicalID,policy,c.ContentSHA256,c.Spans})}
 return HashJSON(struct{Canonical,Policy,Content string;Spans []Span;Structure *Structure}{c.CanonicalID,policy,c.ContentSHA256,c.Spans,c.Structure})
}
