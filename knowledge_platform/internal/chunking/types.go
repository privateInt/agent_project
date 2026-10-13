// Package chunking is deterministic and has no network, model or storage dependency.
package chunking

import (
 "bytes"
 "crypto/sha256"
 "encoding/hex"
 "encoding/json"
 "fmt"
 "io"
 "sort"
 "unicode/utf8"
)

const Version = "chunk-set-v1"
const Implementation = "2026-09-21.1"
const PreviousImplementation = "2026-09-18.2"
const LegacyImplementation = "2026-09-18.1"
const MaxInputBytes = 32 << 20
const MaxOutputBytes = 64 << 20
const MaxDocuments = 20
const MaxChunks = 20000

type Options struct {
 MaxCharacters int `json:"max_characters"`
 OverlapCharacters int `json:"overlap_characters"`
}
type Selection struct {
 ID string `json:"id"`
 Version string `json:"version"`
 Options Options `json:"options"`
 PolicyHash string `json:"policy_hash"`
}
type Input struct {
 CanonicalID string `json:"canonical_id"`
 CanonicalSHA256 string `json:"canonical_sha256"`
 SizeBytes int64 `json:"size_bytes"`
 SourceDocumentID string `json:"source_document_id"`
 SourceSHA256 string `json:"source_sha256"`
 RefinementRunID string `json:"refinement_run_id"`
 SchemaVersion string `json:"schema_version"`
 ProcessorVersion string `json:"processor_version"`
 RulesVersion string `json:"rules_version"`
 ProcessorImageID string `json:"processor_image_id"`
 Format string `json:"format"`
}
type Spec struct {
 SchemaVersion string `json:"schema_version"`
 SnapshotID string `json:"snapshot_id"`
 Documents []Input `json:"documents"`
 Chunker Selection `json:"chunker"`
}
type Span struct {
 BlockID string `json:"block_id"`
 Start int `json:"start"`
 End int `json:"end"`
 ChunkStart int `json:"chunk_start"`
 ChunkEnd int `json:"chunk_end"`
 Metadata json.RawMessage `json:"metadata"` // all original block fields except text; location/cells remain intact
}
type Chunk struct {
 ID string `json:"chunk_id"`
 SourceDocumentID string `json:"source_document_id"`
 CanonicalID string `json:"canonical_id"`
 Ordinal int `json:"ordinal"`
 Text string `json:"text"`
 ContentSHA256 string `json:"content_sha256"`
 Kind string `json:"kind"`
 Spans []Span `json:"source_spans"`
 Quality json.RawMessage `json:"quality"`
 Structure *Structure `json:"structure,omitempty"`
}
type Exclusion struct {
 CanonicalID string `json:"canonical_id"`
 BlockID string `json:"block_id"`
 Reason string `json:"reason"`
 Metadata json.RawMessage `json:"metadata"`
}
type DocumentSummary struct {
 CanonicalID string `json:"canonical_id"`
 Quality json.RawMessage `json:"quality"`
 Pages json.RawMessage `json:"pages"`
 BlockCount int `json:"block_count"`
 CoveredCharacters int `json:"covered_characters"`
 ExcludedBlocks int `json:"excluded_blocks"`
}
type Result struct {
 SchemaVersion string `json:"schema_version"`
 SnapshotID string `json:"snapshot_id"`
 SpecSHA256 string `json:"spec_sha256"`
 Chunker Selection `json:"chunker"`
 Documents []DocumentSummary `json:"documents"`
 Chunks []Chunk `json:"chunks"`
 Exclusions []Exclusion `json:"exclusions"`
}
type Descriptor struct {
 ID string `json:"id"`
 Version string `json:"version"`
 Name string `json:"name"`
 Description string `json:"description"`
 Defaults Options `json:"defaults"`
 MinCharacters int `json:"min_characters"`
 MaxCharacters int `json:"max_characters"`
}
func Normalize(id string, raw []byte) (Selection,error) {
 strategy,ok:=lookup(id);if !ok{return Selection{},fmt.Errorf("unsupported chunker")}
 return normalizeVersion(id,raw,strategy.Describe().Version)
}
func normalizeVersion(id string, raw []byte, version string) (Selection,error) {
 strategy,ok:=lookupVersion(id,version);if !ok { return Selection{},fmt.Errorf("unsupported chunker implementation") }
 o:=strategy.Describe().Defaults
 if len(raw)>0 {
  var fields map[string]json.RawMessage
  if err:=Decode(raw,&fields);err!=nil || fields==nil {return Selection{},fmt.Errorf("options must be an object")}
  for k,v:=range fields {
   var n int
   if bytes.Equal(bytes.TrimSpace(v),[]byte("null")) || json.Unmarshal(v,&n)!=nil {return Selection{},fmt.Errorf("options must be integers")}
   switch k {case "max_characters":o.MaxCharacters=n;case "overlap_characters":o.OverlapCharacters=n;default:return Selection{},fmt.Errorf("unknown chunker option: %s",k)}
  }
 }
 if o.MaxCharacters<100 || o.MaxCharacters>8000 || o.OverlapCharacters<0 || o.OverlapCharacters*2>=o.MaxCharacters {return Selection{},fmt.Errorf("max_characters must be 100..8000; overlap must be nonnegative and less than half the maximum")}
 s:=Selection{ID:id,Version:version,Options:o}
 s.PolicyHash=HashJSON(s)
 return s,nil
}
func Digest(raw []byte) string {s:=sha256.Sum256(raw);return hex.EncodeToString(s[:])}
func HashJSON(v any) string {b,err:=json.Marshal(v);if err!=nil {panic(err)};return Digest(b)}
func NewSpec(inputs []Input, selection Selection) (Spec,error) {
 if len(inputs)<1 || len(inputs)>MaxDocuments {return Spec{},fmt.Errorf("choose 1..20 canonical documents")}
 items:=append([]Input(nil),inputs...)
 sort.Slice(items,func(i,j int)bool{return items[i].CanonicalID<items[j].CanonicalID})
 seen:=map[string]bool{};var total int64
 for _,in:=range items {
  if in.CanonicalID=="" || seen[in.CanonicalID] || in.SourceDocumentID=="" || in.SizeBytes<1 || in.SizeBytes>MaxInputBytes || !ValidDigest(in.CanonicalSHA256) || !ValidDigest(in.SourceSHA256) || in.SchemaVersion!="canonical-document-v1" {return Spec{},fmt.Errorf("invalid or duplicate canonical input")}
  seen[in.CanonicalID]=true;total+=in.SizeBytes
 }
 if total>MaxInputBytes {return Spec{},fmt.Errorf("combined canonical input exceeds 32 MiB")}
 s:=Spec{SchemaVersion:"chunking-spec-v1",Documents:items,Chunker:selection}
 s.SnapshotID=HashJSON(items)
 return s,nil
}
func ValidDigest(s string) bool {if len(s)!=64{return false};for _,r:=range s{if !(r>='0'&&r<='9'||r>='a'&&r<='f'){return false}};return true}

// Reject duplicate JSON keys before decoding; otherwise different consumers may
// disagree about source identity. Bound nesting independently of byte size.
func unique(d *json.Decoder, depth int) error {
 if depth>128{return fmt.Errorf("JSON nesting limit exceeded")}
 t,e:=d.Token();if e!=nil{return e};delim,ok:=t.(json.Delim);if !ok{return nil}
 if delim=='{' {seen:=map[string]bool{};for d.More(){k,e:=d.Token();if e!=nil{return e};key,ok:=k.(string);if !ok||seen[key]{return fmt.Errorf("duplicate JSON key")};seen[key]=true;if e=unique(d,depth+1);e!=nil{return e}}} else if delim=='[' {for d.More(){if e=unique(d,depth+1);e!=nil{return e}}} else {return fmt.Errorf("invalid JSON delimiter")}
 _,e=d.Token();return e
}
func Decode(raw []byte,out any) error {
 if !utf8.Valid(raw){return fmt.Errorf("invalid UTF-8")}
 d:=json.NewDecoder(bytes.NewReader(raw));d.UseNumber()
 if e:=unique(d,0);e!=nil{return e};if _,e:=d.Token();e!=io.EOF{return fmt.Errorf("trailing JSON")}
 d=json.NewDecoder(bytes.NewReader(raw));d.DisallowUnknownFields();return d.Decode(out)
}
