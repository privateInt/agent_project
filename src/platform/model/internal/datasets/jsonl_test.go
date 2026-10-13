package datasets

import (
	"bytes"
	"crypto/sha256"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const sampleRecord = `{"messages":[{"role":"user","content":"질문"},{"role":"assistant","content":"답변"}]}`

func TestInspectExactBytesAndAllRecords(t *testing.T) {
	data := sampleRecord+"\r\n\n"+sampleRecord
	info, err := Inspect(strings.NewReader(data))
	if err != nil { t.Fatal(err) }
	if info.SHA256 != fmt.Sprintf("%x", sha256.Sum256([]byte(data))) || info.SizeBytes != int64(len(data)) || info.RecordCount != 2 { t.Fatalf("wrong content metadata: %+v", info) }
	for name, data := range map[string]string{
		"empty": "", "single": sampleRecord, "malformed-after-20": strings.Repeat(sampleRecord+"\n",20)+"{broken}\n",
		"invalid-utf8": strings.ReplaceAll(sampleRecord,"질문",string([]byte{0xff}))+"\n"+sampleRecord,
		"null-content": strings.ReplaceAll(sampleRecord,`"답변"`,`null`)+"\n"+sampleRecord,
		"wrong-case": strings.ReplaceAll(sampleRecord,`"messages"`,`"Messages"`)+"\n"+sampleRecord,
		"wrong-content-case": strings.ReplaceAll(sampleRecord,`"content"`,`"Content"`)+"\n"+sampleRecord,
		"missing-assistant": strings.ReplaceAll(sampleRecord,`"assistant"`,`"user"`)+"\n"+sampleRecord,
		"unsupported-role": strings.ReplaceAll(sampleRecord,`"user"`,`"tool"`)+"\n"+sampleRecord,
		"unsupported-fields": `{"messages":[{"role":"user","content":"q","tool_calls":[]}]} `+"\n"+sampleRecord,
		"multiple-json-values": sampleRecord+sampleRecord+"\n"+sampleRecord,
		"oversized-record": strings.ReplaceAll(sampleRecord,"질문",strings.Repeat("x",MaxRecordBytes))+"\n"+sampleRecord,
	} { t.Run(name,func(t *testing.T){ if _,err:=Inspect(strings.NewReader(data));err==nil { t.Fatal("invalid dataset accepted") } }) }
}

func TestVerifyRejectsChangedContentAndMetadata(t *testing.T) {
	data := strings.Repeat(sampleRecord+"\n",2)
	info, err := Inspect(strings.NewReader(data)); if err != nil { t.Fatal(err) }
	if err := Verify(strings.NewReader(data),info); err != nil { t.Fatal(err) }
	for _, modified := range []string{strings.ReplaceAll(data,"답변","대답"), data+" ", data[:len(data)-1]} {
		if err:=Verify(strings.NewReader(modified),info);err==nil { t.Fatal("changed bytes accepted") }
	}
	for _, bad := range []Info{{SHA256:info.SHA256,SizeBytes:info.SizeBytes,RecordCount:3},{SHA256:strings.Repeat("0",64),SizeBytes:info.SizeBytes,RecordCount:2},{SHA256:info.SHA256,SizeBytes:MaxBytes+1,RecordCount:2},{}} {
		if err:=Verify(strings.NewReader(data),bad);err==nil { t.Fatal("invalid pin accepted") }
	}
}

func TestMaterializeNeverPublishesUnverifiedFile(t *testing.T) {
	data := []byte(strings.Repeat(sampleRecord+"\n",2))
	info, err := Inspect(bytes.NewReader(data)); if err != nil { t.Fatal(err) }
	dest := filepath.Join(t.TempDir(),"train.jsonl")
	if err:=Materialize(strings.NewReader("bad"),dest,info);err==nil { t.Fatal("corrupt file accepted") }
	if _,err:=os.Stat(dest);!os.IsNotExist(err) { t.Fatalf("partial file exposed: %v",err) }
	if err:=Materialize(bytes.NewReader(data),dest,info);err!=nil { t.Fatal(err) }
	got,err:=os.ReadFile(dest);if err!=nil || !bytes.Equal(got,data) { t.Fatal("verified file changed") }
	files,err:=os.ReadDir(filepath.Dir(dest));if err!=nil || len(files)!=1 { t.Fatal("temporary files leaked") }
}

func TestTotalSizeLimit(t *testing.T) {
	// Many individually small records exercise the total cap, not the line cap.
	record:=strings.ReplaceAll(sampleRecord,"질문",strings.Repeat("x",4096))+"\n"
	input:=strings.NewReader(strings.Repeat(record,MaxBytes/len(record)+1))
	if _,err:=Inspect(input);err==nil { t.Fatal("oversized dataset accepted") }
}
