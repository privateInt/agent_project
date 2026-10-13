package chunking

import (
 "bytes"
 "encoding/json"
 "fmt"
 "sort"
 "strings"
 "unicode/utf8"
)

type block struct {ID string;Kind string;Recognition string;Text []rune;Metadata json.RawMessage}
type segment struct {Block block;Start int;End int}
type document struct {Blocks []block;Quality json.RawMessage;Pages json.RawMessage}

func readDocument(raw []byte,in Input) (document,error) {
 bad:=func()(document,error){return document{},fmt.Errorf("canonical identity or structure mismatch: %s",in.CanonicalID)}
 if int64(len(raw))!=in.SizeBytes || Digest(raw)!=in.CanonicalSHA256{return bad()}
 var fields map[string]json.RawMessage
 if Decode(raw,&fields)!=nil{return bad()}
 var schema string;var source struct{DocumentID string `json:"document_id"`;SHA string `json:"sha256"`;Format string `json:"format"`}
 var processor struct{Version string `json:"version"`;Rules string `json:"rules_version"`;Image string `json:"image_id"`}
 if json.Unmarshal(fields["schema_version"],&schema)!=nil || schema!=in.SchemaVersion || json.Unmarshal(fields["source"],&source)!=nil || source.DocumentID!=in.SourceDocumentID || source.SHA!=in.SourceSHA256 || source.Format!=in.Format || json.Unmarshal(fields["processor"],&processor)!=nil || processor.Version!=in.ProcessorVersion || processor.Rules!=in.RulesVersion || processor.Image!=in.ProcessorImageID {return bad()}
 var quality struct{State string `json:"state"`;Issues []json.RawMessage `json:"issues"`}
 var pages []json.RawMessage;var blocks []map[string]json.RawMessage
 if json.Unmarshal(fields["quality"],&quality)!=nil || quality.Issues==nil || (quality.State!="ready"&&quality.State!="needs_review"&&quality.State!="needs_ocr") || json.Unmarshal(fields["pages"],&pages)!=nil || pages==nil || json.Unmarshal(fields["blocks"],&blocks)!=nil || blocks==nil || len(blocks)>100000{return bad()}
 d:=document{Quality:fields["quality"],Pages:fields["pages"]}
 for i,f:=range blocks {
  var id,kind,text,recognition string;var order int
  if json.Unmarshal(f["id"],&id)!=nil || id!=fmt.Sprintf("b%d",i+1) || json.Unmarshal(f["order"],&order)!=nil || order!=i || json.Unmarshal(f["type"],&kind)!=nil{return bad()}
  if t,ok:=f["text"];ok && json.Unmarshal(t,&text)!=nil{return bad()}
  if r,ok:=f["recognition_status"];ok && json.Unmarshal(r,&recognition)!=nil{return bad()}
  delete(f,"text");meta,e:=json.Marshal(f);if e!=nil{return bad()}
  d.Blocks=append(d.Blocks,block{id,kind,recognition,[]rune(text),meta})
 }
 return d,nil
}

// boundaries recursively applies paragraph, line and word separators only to
// oversized pieces. Returned end positions include separators, so no source
// characters disappear when windows are assembled with overlap.
func boundaries(text []rune,start,end,maximum int,separators []string) []int {
 if end-start<=maximum{return []int{end}}
 if len(separators)==0 {out:=[]int{};for at:=start+maximum;at<end;at+=maximum{out=append(out,at)};return append(out,end)}
 sep:=separators[0];remaining:=string(text[start:end]);at:=start;ends:=[]int{}
 for {index:=strings.Index(remaining,sep);if index<0{break};size:=index+len(sep);at+=utf8.RuneCountInString(remaining[:size]);ends=append(ends,at);remaining=remaining[size:]}
 if len(ends)==0{return boundaries(text,start,end,maximum,separators[1:])}
 if ends[len(ends)-1]!=end{ends=append(ends,end)}
 out:=[]int{};at=start
 for _,next:=range ends {out=append(out,boundaries(text,at,next,maximum,separators[1:])...);at=next}
 return out
}
func split(text []rune,maximum,overlap int) [][2]int {
 cuts:=boundaries(text,0,len(text),maximum,[]string{"\n\n","\n","。",". "," ","\t"})
 var out [][2]int
 for start:=0;start<len(text); {
  if len(text)-start<=maximum{out=append(out,[2]int{start,len(text)});break}
  limit:=start+maximum;end:=limit
  i:=sort.SearchInts(cuts,limit+1)-1
  if i>=0 && cuts[i]>start+max(overlap,maximum/2){end=cuts[i]}
  out=append(out,[2]int{start,end});start=end-overlap
 }
 return out
}

func Build(spec Spec,raws map[string][]byte) (*Result,error) {
 normalized,e:=normalizeVersion(spec.Chunker.ID,mustOptions(spec.Chunker.Options),spec.Chunker.Version);if e!=nil || normalized!=spec.Chunker{return nil,fmt.Errorf("chunker implementation or policy mismatch")}
 strategy,_:=lookupVersion(spec.Chunker.ID,spec.Chunker.Version)
 expected,e:=NewSpec(spec.Documents,spec.Chunker);if e!=nil || HashJSON(expected)!=HashJSON(spec){return nil,fmt.Errorf("input snapshot mismatch")}
 if len(raws)!=len(spec.Documents){return nil,fmt.Errorf("canonical input count mismatch")}
 out:=&Result{SchemaVersion:Version,SnapshotID:spec.SnapshotID,SpecSHA256:HashJSON(spec),Chunker:spec.Chunker,Documents:[]DocumentSummary{},Chunks:[]Chunk{},Exclusions:[]Exclusion{}}
 outputBytes:=0
 for _,in:=range spec.Documents {
  d,e:=readDocument(raws[in.CanonicalID],in);if e!=nil{return nil,e}
  summary:=DocumentSummary{CanonicalID:in.CanonicalID,Quality:d.Quality,Pages:d.Pages,BlockCount:len(d.Blocks)}
  covered:=map[string][][2]int{};excluded:=map[string]bool{}
  var text []rune;var segments []segment
  appendChunk:=func(c Chunk)error {
   for _,span:=range c.Spans {covered[span.BlockID]=append(covered[span.BlockID],[2]int{span.Start,span.End})}
   c.ContentSHA256=Digest([]byte(c.Text))
   c.ID=chunkID(c,spec.Chunker.PolicyHash)
   encoded,e:=json.Marshal(c);if e!=nil{return e};outputBytes+=len(encoded)+1
   if outputBytes>MaxOutputBytes{return fmt.Errorf("chunk output exceeds 64 MiB")}
   out.Chunks=append(out.Chunks,c)
   if len(out.Chunks)>MaxChunks{return fmt.Errorf("chunk count exceeds 20000")}
   return nil
  }
  emit:=func(kind string)error {
   for _,span:=range strategy.Split(text,segments,spec.Chunker.Options) {
    c:=Chunk{SourceDocumentID:in.SourceDocumentID,CanonicalID:in.CanonicalID,Ordinal:len(out.Chunks),Text:string(text[span[0]:span[1]]),Kind:kind,Spans:[]Span{},Quality:d.Quality}
    first:=sort.Search(len(segments),func(i int)bool{return segments[i].End>span[0]})
    for i:=first;i<len(segments)&&segments[i].Start<span[1];i++ {
     s:=segments[i]
     start,end:=max(span[0],s.Start),min(span[1],s.End);if start>=end{continue}
     c.Spans=append(c.Spans,Span{BlockID:s.Block.ID,Start:start-s.Start,End:end-s.Start,ChunkStart:start-span[0],ChunkEnd:end-span[0],Metadata:s.Block.Metadata})
    }
    if len(c.Spans)==0{return fmt.Errorf("chunk lacks source spans")}
    if e:=appendChunk(c);e!=nil{return e}
   }
   text=nil;segments=nil;return nil
  }
  exclude:=func(b block,reason string){excluded[b.ID]=true;summary.ExcludedBlocks++;out.Exclusions=append(out.Exclusions,Exclusion{in.CanonicalID,b.ID,reason,b.Metadata})}
  for _,b:=range d.Blocks {
   textual:=b.Kind=="text"||b.Kind=="markdown"||b.Kind=="paragraph"||b.Kind=="heading"
   if (b.Kind=="image"||b.Kind=="unhandled_structure") && b.Recognition!="unread" && strings.TrimSpace(string(b.Text))!="" {textual=true}
   structured:=b.Kind=="table"||b.Kind=="code"
   if !textual && !structured {
    if e=emit("text");e!=nil{return nil,e}
    // Keep actual OCR text even on image/unhandled blocks above. Unread image
    // placeholders remain explicit exclusions, never searchable.
    exclude(b,"unsupported_or_unrecognized_structure");continue
   }
   if len(b.Text)==0 || strings.TrimSpace(string(b.Text))=="" {
    if e=emit("text");e!=nil{return nil,e};exclude(b,"empty_or_whitespace");continue
   }
   if structured {
    if e=emit("text");e!=nil{return nil,e}
    if len(b.Text)>spec.Chunker.Options.MaxCharacters {
     if spec.Chunker.Version==LegacyImplementation{return nil,fmt.Errorf("STRUCTURED_BLOCK_TOO_LARGE: %s/%s; increase max_characters or review the source",in.CanonicalID,b.ID)}
     fragments,err:=structureFragments(b,spec.Chunker.Options.MaxCharacters);if err!=nil{return nil,fmt.Errorf("canonical=%s: %w",in.CanonicalID,err)}
     for _,f:=range fragments {
      info:=f.info
      c:=Chunk{SourceDocumentID:in.SourceDocumentID,CanonicalID:in.CanonicalID,Ordinal:len(out.Chunks),Kind:b.Kind,Quality:d.Quality,Structure:&info,Spans:[]Span{}}
      var content []rune
      for _,r:=range f.ranges {
       begin:=len(content);content=append(content,b.Text[r[0]:r[1]]...)
       c.Spans=append(c.Spans,Span{BlockID:b.ID,Start:r[0],End:r[1],ChunkStart:begin,ChunkEnd:len(content),Metadata:b.Metadata})
      }
      c.Text=string(content);if err=appendChunk(c);err!=nil{return nil,err}
     }
     continue
    }
    text=b.Text;segments=[]segment{{b,0,len(b.Text)}};if e=emit(b.Kind);e!=nil{return nil,e};continue
   }
   if strategy.FlushBefore(segments,b,spec.Chunker.Options) {if e=emit("text");e!=nil{return nil,e}}
   if len(text)>0{text=append(text,'\n','\n')}
   start:=len(text);text=append(text,b.Text...);segments=append(segments,segment{b,start,len(text)})
  }
  if e=emit("text");e!=nil{return nil,e}
  for _,b:=range d.Blocks {
   if excluded[b.ID]{continue}
   ranges:=covered[b.ID];sort.Slice(ranges,func(i,j int)bool{return ranges[i][0]<ranges[j][0]})
   end:=0
   for _,r:=range ranges {if r[0]>end || r[1]>len(b.Text){return nil,fmt.Errorf("source coverage gap")};end=max(end,r[1])}
   if end!=len(b.Text){return nil,fmt.Errorf("source coverage incomplete")};summary.CoveredCharacters+=end
  }
  out.Documents=append(out.Documents,summary)
 }
 if len(out.Chunks)==0{return nil,fmt.Errorf("NO_SEARCHABLE_CONTENT: input has no readable text")}
 b,e:=json.Marshal(out);if e!=nil || len(b)>MaxOutputBytes{return nil,fmt.Errorf("chunk output exceeds 64 MiB")}
 return out,nil
}
func mustOptions(o Options) []byte {raw,_:=json.Marshal(o);return raw}

// Verify checks the persisted manifest, per-chunk identity and coverage
// consistency without fetching mutable/latest Data. Build verifies source bytes.
func Verify(raw []byte,spec Spec) (*Result,error) {
 normalized,e:=normalizeVersion(spec.Chunker.ID,mustOptions(spec.Chunker.Options),spec.Chunker.Version)
 if e!=nil||normalized!=spec.Chunker{return nil,fmt.Errorf("chunker implementation or policy mismatch")}
 var r Result
 if len(raw)>MaxOutputBytes || Decode(raw,&r)!=nil || r.SchemaVersion!=Version || r.SnapshotID!=spec.SnapshotID || r.SpecSHA256!=HashJSON(spec) || r.Chunker!=spec.Chunker || len(r.Chunks)<1 || len(r.Chunks)>MaxChunks || len(r.Documents)!=len(spec.Documents){return nil,fmt.Errorf("chunk result manifest mismatch")}
 docs:=map[string]string{};seen:=map[string]bool{};summaries:=map[string]DocumentSummary{}
 ranges:=map[string]map[string][][2]int{};metadata:=map[string]string{};excluded:=map[string]map[string]bool{}
 fragments:=map[string]Structure{}
 for i,in:=range spec.Documents {if r.Documents[i].CanonicalID!=in.CanonicalID{return nil,fmt.Errorf("result document mismatch")};docs[in.CanonicalID]=in.SourceDocumentID;summaries[in.CanonicalID]=r.Documents[i];ranges[in.CanonicalID]=map[string][][2]int{};excluded[in.CanonicalID]=map[string]bool{}}
 for i,c:=range r.Chunks {
  if docs[c.CanonicalID]=="" || c.SourceDocumentID!=docs[c.CanonicalID] || seen[c.ID] || c.Ordinal!=i || c.ContentSHA256!=Digest([]byte(c.Text)) || len([]rune(c.Text))>spec.Chunker.Options.MaxCharacters || len(c.Spans)==0 || !bytes.Equal(c.Quality,summaries[c.CanonicalID].Quality){return nil,fmt.Errorf("invalid chunk identity")}
  seen[c.ID]=true
  expected:=chunkID(c,spec.Chunker.PolicyHash)
  if c.ID!=expected{return nil,fmt.Errorf("chunk ID mismatch")}
  if c.Structure!=nil {
   st:=c.Structure
   expectedUnit:="rows";if c.Kind=="code"{expectedUnit="lines"}
   if (spec.Chunker.Version!=Implementation&&spec.Chunker.Version!=PreviousImplementation)||(c.Kind!="table"&&c.Kind!="code")||st.Unit!=expectedUnit||st.Start<0||st.End<=st.Start||st.Part<1||st.Parts<2||st.Part>st.Parts{return nil,fmt.Errorf("invalid structure fragment")}
   for i,row:=range st.ContextRows {if row<0||row>=st.Start||(i>0&&row<=st.ContextRows[i-1]){return nil,fmt.Errorf("invalid structure context")}}
   if len(c.Spans)!=len(st.ContextRows)+1{return nil,fmt.Errorf("fragment context span count mismatch")}
   for _,sp:=range c.Spans{if sp.BlockID!=c.Spans[0].BlockID{return nil,fmt.Errorf("fragment crosses blocks")}}
   key:=c.CanonicalID+"/"+c.Spans[0].BlockID
   prior,exists:=fragments[key]
   if (!exists&&st.Part!=1)||(exists&&(st.Part!=prior.Part+1||st.Parts!=prior.Parts||st.Unit!=prior.Unit||st.Start<prior.End)){return nil,fmt.Errorf("fragment sequence mismatch")}
   fragments[key]=*st
  }
  previous:=0
  for _,s:=range c.Spans {
   if c.Structure!=nil&&s.ChunkStart!=previous{return nil,fmt.Errorf("fragment contains unmapped text")}
   if s.Start<0 || s.End<=s.Start || s.ChunkStart<previous || s.ChunkEnd>len([]rune(c.Text)) || s.ChunkEnd-s.ChunkStart!=s.End-s.Start || !json.Valid(s.Metadata){return nil,fmt.Errorf("invalid source span")};previous=s.ChunkEnd
   var m struct{ID string `json:"id"`;Order int `json:"order"`}
   if json.Unmarshal(s.Metadata,&m)!=nil || m.ID!=s.BlockID || m.Order<0 || m.Order>=summaries[c.CanonicalID].BlockCount || s.BlockID!=fmt.Sprintf("b%d",m.Order+1){return nil,fmt.Errorf("invalid block reference")}
   key:=c.CanonicalID+"/"+s.BlockID;hash:=Digest(s.Metadata)
   if old:=metadata[key];old!=""&&old!=hash{return nil,fmt.Errorf("inconsistent block metadata")};metadata[key]=hash
   ranges[c.CanonicalID][s.BlockID]=append(ranges[c.CanonicalID][s.BlockID],[2]int{s.Start,s.End})
  }
  if c.Structure!=nil&&previous!=len([]rune(c.Text)){return nil,fmt.Errorf("fragment contains unmapped suffix")}
 }
 for _,st:=range fragments{if st.Part!=st.Parts{return nil,fmt.Errorf("missing structure fragment")}}
 for _,e:=range r.Exclusions {
  if docs[e.CanonicalID]==""||e.BlockID==""||e.Reason==""||excluded[e.CanonicalID][e.BlockID]||len(ranges[e.CanonicalID][e.BlockID])>0{return nil,fmt.Errorf("invalid block exclusion")}
  var m struct{ID string `json:"id"`;Order int `json:"order"`};if json.Unmarshal(e.Metadata,&m)!=nil||m.ID!=e.BlockID||m.Order<0||m.Order>=summaries[e.CanonicalID].BlockCount||m.ID!=fmt.Sprintf("b%d",m.Order+1){return nil,fmt.Errorf("invalid excluded block")};excluded[e.CanonicalID][e.BlockID]=true
 }
 for id,summary:=range summaries {
  covered:=0
  for _,spans:=range ranges[id]{sort.Slice(spans,func(i,j int)bool{return spans[i][0]<spans[j][0]});end:=0;for _,s:=range spans{if s[0]>end{return nil,fmt.Errorf("persisted source coverage gap")};end=max(end,s[1])};covered+=end}
  if covered!=summary.CoveredCharacters||len(excluded[id])!=summary.ExcludedBlocks||len(ranges[id])+len(excluded[id])!=summary.BlockCount{return nil,fmt.Errorf("persisted coverage summary mismatch")}
 }
 canonical,_:=json.Marshal(&r)
 if !bytes.Equal(canonical,raw){return nil,fmt.Errorf("noncanonical chunk result encoding")}
 return &r,nil
}
