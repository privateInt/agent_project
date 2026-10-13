package sourcefile

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/binary"
	"image"
	"image/jpeg"
	"image/png"
	"strings"
	"testing"
)

func officeParts(kind string) map[string]string {
	main, ns, root, ct := "word/document.xml", "wordprocessingml", "document", "wordprocessingml.document"
	if kind == "xlsx" {
		main = "xl/workbook.xml"
		ns = "spreadsheetml"
		root = "workbook"
		ct = "spreadsheetml.sheet"
	}
	return map[string]string{
		"[Content_Types].xml": `<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/` + main + `" ContentType="application/vnd.openxmlformats-officedocument.` + ct + `.main+xml"/></Types>`,
		"_rels/.rels":         `<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="` + main + `"/></Relationships>`,
		main:                  `<x:` + root + ` xmlns:x="http://schemas.openxmlformats.org/` + ns + `/2006/main"/>`,
	}
}
func zipped(t *testing.T, parts map[string]string) []byte {
	t.Helper()
	var b bytes.Buffer
	w := zip.NewWriter(&b)
	for n, s := range parts {
		f, e := w.Create(n)
		if e != nil {
			t.Fatal(e)
		}
		if _, e = f.Write([]byte(s)); e != nil {
			t.Fatal(e)
		}
	}
	if e := w.Close(); e != nil {
		t.Fatal(e)
	}
	return b.Bytes()
}
func TestSupportedSourceFormatsPreserveOriginal(t *testing.T) {
	var p, j bytes.Buffer
	if e := png.Encode(&p, image.NewRGBA(image.Rect(0, 0, 2, 3))); e != nil {
		t.Fatal(e)
	}
	if e := jpeg.Encode(&j, image.NewRGBA(image.Rect(0, 0, 2, 3)), nil); e != nil {
		t.Fatal(e)
	}
	for _, tc := range []struct {
		name, format string
		data         []byte
	}{
		{"원본.TXT", "text", []byte("원본\r\n  공백\t유지\n")},
		{"note.md", "markdown", []byte("# title\n\n  code\n")},
		{"source.html", "html", []byte("<html><body>source</body></html>")},
		{"scan.pdf", "pdf", []byte("%PDF-1.7\n1 0 obj <<>> endobj\n%%EOF\n")},
		{"scan.png", "png", p.Bytes()}, {"scan.JPEG", "jpeg", j.Bytes()},
		{"document.docx", "docx", zipped(t, officeParts("docx"))}, {"table.xlsx", "xlsx", zipped(t, officeParts("xlsx"))},
	} {
		t.Run(tc.format, func(t *testing.T) {
			before := bytes.Clone(tc.data)
			info, e := Recognize(context.Background(), tc.name, tc.data)
			if e != nil || info.Format != tc.format || info.ContentType == "" {
				t.Fatal(info, e)
			}
			if !bytes.Equal(tc.data, before) {
				t.Fatal("original changed")
			}
		})
	}
}
func TestInvalidSourceAndFilenameRejected(t *testing.T) {
	for _, tc := range []struct {
		name string
		data []byte
	}{
		{"empty.txt", nil}, {"a.exe", []byte("binary")}, {"../a.txt", []byte("text")}, {"a\\b.txt", []byte("text")}, {"x\r\n.txt", []byte("text")},
		{"utf16.txt", []byte{0xff, 0xfe, 65, 0}}, {"binary.txt", []byte("a\x00b")}, {"fake.pdf", []byte("plain text")},
		{"fake.png", []byte("plain text")}, {"fake.html", []byte("plain text")},
		{"renamed.txt", []byte("%PDF-1.7\n%%EOF")}, {"archive.docx", zipped(t, map[string]string{"x": "y"})},
		{"wrong.xlsx", zipped(t, officeParts("docx"))}, {"too-large.txt", bytes.Repeat([]byte("a"), int(MaxBytes)+1)},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if _, e := Recognize(context.Background(), tc.name, tc.data); e == nil {
				t.Fatal("invalid original accepted")
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, e := Recognize(ctx, "a.txt", []byte("text")); e == nil {
		t.Fatal("cancel ignored")
	}
}
func TestPDFIntakeDoesNotRequireACompleteParseableDocument(t *testing.T) {
	for name, data := range map[string][]byte{
		"missing-eof":     []byte("%PDF-1.7\n1 0 obj <<>> endobj\nstartxref\n12"),
		"leading-crlf":    []byte("\r\n\r\n\r\n\r\n%PDF-1.7\ntruncated"),
		"header-only":     []byte("%PDF-1.7"),
		"bom":             append([]byte{0xef, 0xbb, 0xbf}, []byte("\n%PDF-2.0\n")...),
		"prefix-boundary": append(bytes.Repeat([]byte(" "), 1024), []byte("%PDF-1.4\n")...),
	} {
		t.Run(name, func(t *testing.T) {
			before := bytes.Clone(data)
			info, e := Recognize(context.Background(), "problem.pdf", data)
			if e != nil || info.Format != "pdf" {
				t.Fatal(info, e)
			}
			if !bytes.Equal(data, before) {
				t.Fatal("intake modified original")
			}
		})
	}
	for name, data := range map[string][]byte{
		"plain": []byte("not a PDF"), "short": []byte("%PDF-"), "bad-version": []byte("%PDF-9.9\n"),
		"embedded": []byte("executable %PDF-1.7\n"), "header-suffix": []byte("%PDF-1.7not-pdf"),
		"prefix-over-limit": append(bytes.Repeat([]byte(" "), 1025), []byte("%PDF-1.4\n")...),
	} {
		t.Run(name, func(t *testing.T) {
			if _, e := Recognize(context.Background(), "invalid.pdf", data); e == nil {
				t.Fatal("unidentified source accepted")
			}
		})
	}
	if _, e := Recognize(context.Background(), "renamed.txt", []byte("\n%PDF-1.7\ntruncated")); e == nil {
		t.Fatal("PDF accepted as text")
	}
}
func TestOfficePackageBoundaries(t *testing.T) {
	cases := map[string]func(map[string]string){
		"traversal":  func(p map[string]string) { p["../escape"] = "bad" },
		"macro":      func(p map[string]string) { p["word/vbaProject.bin"] = "macro" },
		"wrong-root": func(p map[string]string) { p["word/document.xml"] = `<not-a-document/>` },
		"external-main": func(p map[string]string) {
			p["_rels/.rels"] = strings.Replace(p["_rels/.rels"], `Target="word/document.xml"`, `Target="https://example.com/" TargetMode="External"`, 1)
		},
		"doctype": func(p map[string]string) {
			p["word/document.xml"] = `<!DOCTYPE x SYSTEM "file:///etc/passwd">` + p["word/document.xml"]
		},
		"nested-xml": func(p map[string]string) {
			p["word/document.xml"] = `<document>` + strings.Repeat("<x>", 130) + strings.Repeat("</x>", 130) + `</document>`
		},
		"expanded-limit": func(p map[string]string) { p["word/big.xml"] = strings.Repeat("x", int(maxPartBytes)+1) },
		"metadata-limit": func(p map[string]string) { p["[Content_Types].xml"] = strings.Repeat("x", int(maxMetadataBytes)+1) },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			p := officeParts("docx")
			change(p)
			if _, e := Recognize(context.Background(), "d.docx", zipped(t, p)); e == nil {
				t.Fatal("unsafe package accepted")
			}
		})
	}
	t.Run("duplicate", func(t *testing.T) {
		var b bytes.Buffer
		w := zip.NewWriter(&b)
		for i := 0; i < 2; i++ {
			f, _ := w.Create("word/document.xml")
			_, _ = f.Write([]byte("x"))
		}
		_ = w.Close()
		if _, e := Recognize(context.Background(), "d.docx", b.Bytes()); e == nil {
			t.Fatal("duplicate accepted")
		}
	})
	t.Run("checksum", func(t *testing.T) {
		b := bytes.Clone(zipped(t, officeParts("docx")))
		index := bytes.Index(b, []byte("PK\x01\x02"))
		if index < 0 {
			t.Fatal("directory missing")
		}
		binary.LittleEndian.PutUint32(b[index+16:index+20], 0)
		if _, e := Recognize(context.Background(), "d.docx", b); e == nil {
			t.Fatal("corrupted ZIP accepted")
		}
	})
}
