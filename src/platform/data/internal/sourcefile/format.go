// Package sourcefile identifies upload formats without modifying the original.
// Recognition is an intake check, not a guarantee of successful later parsing.
package sourcefile

import (
	"archive/zip"
	"bytes"
	"context"
	"encoding/xml"
	"fmt"
	"image"
	_ "image/jpeg"
	_ "image/png"
	"io"
	"path"
	"strings"
	"unicode"
	"unicode/utf8"
)

const MaxBytes int64 = 32 << 20
const ChunkBytes = 64 << 10
const Version = "source-format-v2"
const maxZipEntries = 4096
const maxExpandedBytes uint64 = 128 << 20
const maxPartBytes uint64 = 32 << 20
const maxMetadataBytes uint64 = 1 << 20

type Info struct{ Format, ContentType string }

// Identify PDF bytes at intake, not whether a text parser can open them. Some
// renderers can read PDFs with leading whitespace or an incomplete trailer.
// Keep those original bytes for Refinement to parse/render; never repair here.
func pdfSignature(data []byte) bool {
	offset := 0
	if bytes.HasPrefix(data, []byte{0xef, 0xbb, 0xbf}) {
		offset = 3
	}
	for offset < len(data) && offset < 1024 && strings.ContainsRune("\x00\t\n\f\r ", rune(data[offset])) {
		offset++
	}
	data = data[offset:]
	if len(data) < 8 {
		return false
	}
	version := bytes.HasPrefix(data, []byte("%PDF-1.")) && data[7] >= '0' && data[7] <= '7' || bytes.HasPrefix(data, []byte("%PDF-2.0"))
	return version && (len(data) == 8 || strings.ContainsRune("\x00\t\n\f\r ", rune(data[8])))
}

func Name(raw string) (string, error) {
	name := strings.TrimSpace(raw)
	if !utf8.ValidString(name) || name == "" || utf8.RuneCountInString(name) > 200 {
		return "", fmt.Errorf("name must contain 1..200 characters")
	}
	for _, r := range name {
		if unicode.IsControl(r) {
			return "", fmt.Errorf("name contains control characters")
		}
	}
	return name, nil
}
func Filename(name string) error {
	if !utf8.ValidString(name) || name == "" || len(name) > 255 || strings.TrimSpace(name) != name || strings.ContainsAny(name, "/\\:") || name == "." || name == ".." {
		return fmt.Errorf("filename must be a basename of 1..255 UTF-8 bytes")
	}
	for _, r := range name {
		if unicode.IsControl(r) {
			return fmt.Errorf("filename contains control characters")
		}
	}
	return nil
}
func Recognize(ctx context.Context, filename string, data []byte) (Info, error) {
	if e := Filename(filename); e != nil {
		return Info{}, e
	}
	if len(data) == 0 || int64(len(data)) > MaxBytes {
		return Info{}, fmt.Errorf("file must contain 1..33554432 bytes")
	}
	if e := ctx.Err(); e != nil {
		return Info{}, e
	}
	bad := func() (Info, error) { return Info{}, fmt.Errorf("file structure does not match its extension") }
	ext := strings.ToLower(path.Ext(filename))
	switch ext {
	case ".txt", ".md", ".markdown", ".html", ".htm":
		if !utf8.Valid(data) {
			return Info{}, fmt.Errorf("text uploads must use UTF-8")
		}
		for _, r := range string(data) {
			if unicode.IsControl(r) && r != '\n' && r != '\r' && r != '\t' && r != '\f' {
				return bad()
			}
		}
		// Renaming recognized binary originals as text must not change their format.
		if pdfSignature(data) || bytes.HasPrefix(data, []byte("%PDF-")) || bytes.HasPrefix(data, []byte("PK\x03\x04")) {
			return bad()
		}
		if ext == ".html" || ext == ".htm" {
			// Basic markup recognition only; extraction/cleaning is a later stage.
			if !bytes.Contains(data, []byte("<")) || !bytes.Contains(data, []byte(">")) {
				return bad()
			}
			return Info{"html", "text/html; charset=utf-8"}, nil
		}
		if ext == ".md" || ext == ".markdown" {
			return Info{"markdown", "text/markdown; charset=utf-8"}, nil
		}
		return Info{"text", "text/plain; charset=utf-8"}, nil
	case ".pdf":
		if !pdfSignature(data) {
			return Info{}, fmt.Errorf("PDF signature was not found after a bounded whitespace prefix")
		}
		return Info{"pdf", "application/pdf"}, nil
	case ".png", ".jpg", ".jpeg":
		config, format, e := image.DecodeConfig(bytes.NewReader(data))
		if e != nil || config.Width < 1 || config.Height < 1 || int64(config.Width)*int64(config.Height) > 100_000_000 {
			return bad()
		}
		if ext == ".png" {
			if format != "png" || !bytes.HasSuffix(data, []byte{0, 0, 0, 0, 'I', 'E', 'N', 'D', 0xae, 0x42, 0x60, 0x82}) {
				return bad()
			}
			return Info{"png", "image/png"}, nil
		}
		if format != "jpeg" || !bytes.HasSuffix(data, []byte{0xff, 0xd9}) {
			return bad()
		}
		return Info{"jpeg", "image/jpeg"}, nil
	case ".docx", ".xlsx":
		return office(ctx, ext, data)
	default:
		return Info{}, fmt.Errorf("unsupported file extension; use PDF, HTML, TXT, MD, PNG, JPEG, DOCX or XLSX")
	}
}

type contextReader struct {
	ctx context.Context
	r   io.Reader
}

func (r contextReader) Read(p []byte) (int, error) {
	if e := r.ctx.Err(); e != nil {
		return 0, e
	}
	return r.r.Read(p)
}

type contentTypes struct {
	XMLName   xml.Name `xml:"http://schemas.openxmlformats.org/package/2006/content-types Types"`
	Overrides []struct {
		PartName string `xml:"PartName,attr"`
		Type     string `xml:"ContentType,attr"`
	} `xml:"Override"`
	Defaults []struct {
		Type string `xml:"ContentType,attr"`
	} `xml:"Default"`
}
type relationships struct {
	XMLName xml.Name `xml:"http://schemas.openxmlformats.org/package/2006/relationships Relationships"`
	Items   []struct {
		Type   string `xml:"Type,attr"`
		Target string `xml:"Target,attr"`
		Mode   string `xml:"TargetMode,attr"`
	} `xml:"Relationship"`
}

// XML is read in a bounded stream. Directives and excessive nesting are rejected;
// entity references are not resolved from files or from the network.
func xmlRoot(data []byte) (xml.Name, error) {
	decoder := xml.NewDecoder(bytes.NewReader(data))
	depth := 0
	var root xml.Name
	ended := false
	for {
		token, e := decoder.Token()
		if e == io.EOF {
			if root.Local == "" || depth != 0 {
				return root, fmt.Errorf("incomplete XML")
			}
			return root, nil
		}
		if e != nil {
			return root, e
		}
		switch t := token.(type) {
		case xml.Directive:
			return root, fmt.Errorf("XML directives are not supported")
		case xml.StartElement:
			if ended {
				return root, fmt.Errorf("multiple XML roots")
			}
			if depth == 0 {
				root = t.Name
			}
			depth++
			if depth > 128 {
				return root, fmt.Errorf("XML nesting limit exceeded")
			}
		case xml.EndElement:
			depth--
			if depth == 0 {
				ended = true
			}
		case xml.CharData:
			if depth == 0 && strings.TrimSpace(string(t)) != "" {
				return root, fmt.Errorf("text outside XML root")
			}
		}
	}
}
func office(ctx context.Context, ext string, data []byte) (Info, error) {
	bad := func(message string) (Info, error) {
		return Info{}, fmt.Errorf("invalid DOCX/XLSX package: %s", message)
	}
	if !bytes.HasPrefix(data, []byte("PK\x03\x04")) {
		return bad("expected an unencrypted OOXML ZIP package")
	}
	zr, e := zip.NewReader(bytes.NewReader(data), int64(len(data)))
	if e != nil {
		return bad("ZIP directory is invalid")
	}
	if len(zr.File) > maxZipEntries {
		return bad("too many ZIP entries")
	}
	main, rootName, namespace, mainType, result := "word/document.xml", "document", "wordprocessingml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml", Info{"docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
	if ext == ".xlsx" {
		main = "xl/workbook.xml"
		rootName = "workbook"
		namespace = "spreadsheetml"
		mainType = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
		result = Info{"xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
	}
	seen := map[string]bool{}
	parts := map[string][]byte{}
	var total uint64
	for _, f := range zr.File {
		if e = ctx.Err(); e != nil {
			return Info{}, e
		}
		name := f.Name
		trimmed := strings.TrimSuffix(name, "/")
		if name == "" || len(name) > 512 || !utf8.ValidString(name) || strings.ContainsAny(name, "\\\x00:") || strings.HasPrefix(name, "/") || path.Clean(trimmed) != trimmed || trimmed == ".." || strings.HasPrefix(trimmed, "../") || seen[trimmed] {
			return bad("unsafe or duplicate part name")
		}
		seen[trimmed] = true
		if f.Flags&1 != 0 || (f.Method != zip.Store && f.Method != zip.Deflate) {
			return bad("encrypted or unsupported ZIP entry")
		}
		// ModeType checks are done separately from portable permission bits.
		if !f.FileInfo().IsDir() && !f.Mode().IsRegular() {
			return bad("non-regular ZIP entry")
		}
		if strings.Contains(strings.ToLower(name), "vbaproject") {
			return bad("macro-enabled files are unsupported")
		}
		if f.UncompressedSize64 > maxPartBytes || f.UncompressedSize64 > maxExpandedBytes-total {
			return bad("expanded size limit exceeded")
		}
		total += f.UncompressedSize64
		wanted := name == "[Content_Types].xml" || name == "_rels/.rels" || name == main
		if wanted && name != main && f.UncompressedSize64 > maxMetadataBytes {
			return bad("package metadata too large")
		}
		reader, e := f.Open()
		if e != nil {
			return bad("unreadable ZIP entry")
		}
		limited := io.LimitReader(contextReader{ctx, reader}, int64(f.UncompressedSize64)+1)
		var n int64
		if wanted {
			part, readErr := io.ReadAll(limited)
			e = readErr
			n = int64(len(part))
			parts[name] = part
		} else {
			n, e = io.Copy(io.Discard, limited)
		}
		closeErr := reader.Close()
		if e != nil || closeErr != nil || n != int64(f.UncompressedSize64) {
			return bad("ZIP size or checksum mismatch")
		}
	}
	for _, name := range []string{"[Content_Types].xml", "_rels/.rels", main} {
		if _, ok := parts[name]; !ok {
			return bad("required document parts are missing")
		}
		if _, e = xmlRoot(parts[name]); e != nil {
			return bad("invalid document XML")
		}
	}
	var types contentTypes
	if e = xml.Unmarshal(parts["[Content_Types].xml"], &types); e != nil {
		return bad("invalid content types")
	}
	found := false
	for _, p := range types.Overrides {
		if strings.Contains(strings.ToLower(p.Type), "macroenabled") || strings.Contains(strings.ToLower(p.Type), "vbaproject") {
			return bad("macro-enabled files are unsupported")
		}
		if p.PartName == "/"+main {
			if found || p.Type != mainType {
				return bad("main document type differs")
			}
			found = true
		}
	}
	for _, p := range types.Defaults {
		if strings.Contains(strings.ToLower(p.Type), "macroenabled") || strings.Contains(strings.ToLower(p.Type), "vbaproject") {
			return bad("macro-enabled files are unsupported")
		}
	}
	if !found {
		return bad("main document type is missing")
	}
	var rels relationships
	if e = xml.Unmarshal(parts["_rels/.rels"], &rels); e != nil {
		return bad("invalid package relationships")
	}
	found = false
	for _, rel := range rels.Items {
		if rel.Type == "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" || rel.Type == "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument" {
			if found || rel.Mode != "" && rel.Mode != "Internal" || strings.TrimPrefix(rel.Target, "/") != main {
				return bad("main relationship differs")
			}
			found = true
		}
	}
	if !found {
		return bad("main relationship is missing")
	}
	root, e := xmlRoot(parts[main])
	if e != nil || root.Local != rootName || (root.Space != "http://schemas.openxmlformats.org/"+namespace+"/2006/main" && root.Space != "http://purl.oclc.org/ooxml/"+namespace+"/main") {
		return bad("main document root differs")
	}
	return result, nil
}
