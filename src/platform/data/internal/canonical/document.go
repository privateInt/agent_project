// Package canonical validates immutable CPU parser output before registration.
package canonical

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"mime"

	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/blob"
	"data-platform/internal/ocr"
	"github.com/google/uuid"
)

const Schema = "canonical-document-v1"
const Processor = "cpu-parser-v1"
const Rules = "basic-cleaning-v1"
const MaxBytes int64 = 32 << 20

type Source struct {
	DocumentID string `json:"document_id"`
	SHA256     string `json:"sha256"`
	Format     string `json:"format"`
}
type Spec struct {
	MeasureTimings bool          `json:"measure_timings,omitempty"`
	OCR            *ocr.Identity `json:"ocr,omitempty"`
	DocumentID     string        `json:"document_id"`
	SHA256         string        `json:"sha256"`
	Format         string        `json:"format"`
	RulesVersion   string        `json:"rules_version"`
	ImageID        string        `json:"image_id"`
}
type Processing struct {
	Version string `json:"version"`
	Mode    string `json:"mode"`
}

type Document struct {
	Processing      *Processing      `json:"processing,omitempty"`
	TableComparison *TableComparison `json:"table_comparison,omitempty"`
	OCR             *OCRResult       `json:"ocr,omitempty"`
	SchemaVersion   string           `json:"schema_version"`
	Source          Source           `json:"source"`
	Processor       struct {
		Version      string `json:"version"`
		RulesVersion string `json:"rules_version"`
		ImageID      string `json:"image_id"`
	} `json:"processor"`
	Blocks []struct {
		VisualRegionID    string          `json:"visual_region_id"`
		ModelBox          []float64       `json:"model_bbox"`
		ModelCoordinates  string          `json:"model_coordinate_system"`
		TableComparisonID string          `json:"table_comparison_id"`
		Cells             json.RawMessage `json:"cells"`
		ID                string          `json:"id"`
		Order             int             `json:"order"`
		Type              string          `json:"type"`
		Text              string          `json:"text"`
		Label             string          `json:"label"`
		RecognitionStatus string          `json:"recognition_status"`
		Location          tableLocation   `json:"location"`
	} `json:"blocks"`
	Pages   []json.RawMessage `json:"pages"`
	Quality struct {
		State  string `json:"state"`
		Issues []struct {
			Code        string `json:"code"`
			RequiresOCR bool   `json:"requires_ocr"`
			Location    struct {
				Page    int    `json:"page"`
				TableID string `json:"table_id"`
			} `json:"location"`
		} `json:"issues"`
	} `json:"quality"`
	Cleaning struct {
		Version string `json:"version"`
	} `json:"cleaning"`
}

func ValidState(s string) bool { return s == "ready" || s == "needs_ocr" || s == "needs_review" }
func Format(d *collectionv1.SourceDocument) (string, error) {
	switch d.GetFormat() {
	case "text", "markdown", "html", "pdf", "png", "jpeg", "docx", "xlsx":
		return d.Format, nil
	}
	t, _, e := mime.ParseMediaType(d.GetContentType())
	if e != nil {
		return "", fmt.Errorf("unsupported source format")
	}
	switch t {
	case "text/plain":
		return "text", nil
	case "text/markdown":
		return "markdown", nil
	case "text/html":
		return "html", nil
	case "application/pdf":
		return "pdf", nil
	case "image/png":
		return "png", nil
	case "image/jpeg":
		return "jpeg", nil
	case "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
		return "docx", nil
	case "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
		return "xlsx", nil
	}
	return "", fmt.Errorf("unsupported source format")
}
func Key(run, digest string) (string, error) {
	id, e := uuid.Parse(run)
	if e != nil || id == uuid.Nil || id.String() != run || !blob.ValidDigest(digest) {
		return "", fmt.Errorf("invalid canonical identity")
	}
	return "canonical/" + run + "/sha256/" + digest + "/document.json", nil
}
func Read(ctx context.Context, s blob.Store, key, digest string, size int64) ([]byte, error) {
	if size < 1 || size > MaxBytes || !blob.ValidDigest(digest) {
		return nil, fmt.Errorf("invalid canonical size or digest")
	}
	r, e := s.Open(ctx, key)
	if e != nil {
		return nil, e
	}
	defer r.Close()
	data, e := io.ReadAll(io.LimitReader(r, size+1))
	if e != nil {
		return nil, e
	}
	sum := sha256.Sum256(data)
	if int64(len(data)) != size || hex.EncodeToString(sum[:]) != digest {
		return nil, fmt.Errorf("canonical checksum mismatch")
	}
	return data, nil
}

// Reject duplicate keys at every depth so consumers cannot disagree about provenance.
func unique(d *json.Decoder, depth int) error {
	if depth > 128 {
		return fmt.Errorf("JSON nesting limit exceeded")
	}
	t, e := d.Token()
	if e != nil {
		return e
	}
	delim, ok := t.(json.Delim)
	if !ok {
		return nil
	}
	if delim == '{' {
		seen := map[string]bool{}
		for d.More() {
			k, e := d.Token()
			if e != nil {
				return e
			}
			key, ok := k.(string)
			if !ok || seen[key] {
				return fmt.Errorf("duplicate key")
			}
			seen[key] = true
			if e = unique(d, depth+1); e != nil {
				return e
			}
		}
	} else if delim == '[' {
		for d.More() {
			if e = unique(d, depth+1); e != nil {
				return e
			}
		}
	} else {
		return fmt.Errorf("invalid delimiter")
	}
	_, e = d.Token()
	return e
}
func Validate(raw []byte, spec Spec) (*Document, error) {
	bad := func() (*Document, error) { return nil, fmt.Errorf("canonical structure or provenance mismatch") }
	dec := json.NewDecoder(bytes.NewReader(raw))
	if unique(dec, 0) != nil {
		return bad()
	}
	if _, e := dec.Token(); e != io.EOF {
		return bad()
	}
	var d Document
	if json.Unmarshal(raw, &d) != nil {
		return bad()
	}
	if d.SchemaVersion != Schema || d.Source != (Source{spec.DocumentID, spec.SHA256, spec.Format}) || !validateOCR(&d, spec) || !validateVisualRegions(&d) || !validateTables(&d) || !validateProcessing(&d, spec) || d.Processor.RulesVersion != Rules || spec.RulesVersion != Rules || d.Processor.ImageID != spec.ImageID || d.Cleaning.Version != Rules || !ValidState(d.Quality.State) || d.Blocks == nil || d.Pages == nil || d.Quality.Issues == nil || len(d.Blocks) > 100000 || len(d.Pages) > 300 {
		return bad()
	}
	for i, b := range d.Blocks {
		if b.ID != fmt.Sprintf("b%d", i+1) || b.Order != i {
			return bad()
		}
		switch b.Type {
		case "text", "markdown", "paragraph", "heading", "code", "table", "image", "unhandled_structure":
		default:
			return bad()
		}
	}
	ocr := false
	for _, issue := range d.Quality.Issues {
		if issue.Code == "" {
			return bad()
		}
		ocr = ocr || issue.RequiresOCR
	}
	expected := "ready"
	if len(d.Quality.Issues) > 0 {
		expected = "needs_review"
	}
	if ocr {
		expected = "needs_ocr"
	}
	if expected != d.Quality.State || len(d.Blocks) == 0 && expected == "ready" {
		return bad()
	}
	return &d, nil
}

func validateProcessing(d *Document, spec Spec) bool {
	if d.Processing == nil {
		return true
	} // Older immutable results remain readable.
	expected := "parser_only"
	if spec.OCR != nil {
		expected = "ocr_priority"
	}
	return d.Processing.Version == "refinement-processing-v1" && d.Processing.Mode == expected
}
