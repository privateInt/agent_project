package canonical

import (
	"data-platform/internal/ocr"
	"encoding/json"
	"fmt"
	"strings"
)

type OCRResult struct {
	Identity      ocr.Identity `json:"identity"`
	ClientVersion string       `json:"client_version"`
	Policy        string       `json:"policy"`
	Pages         []struct {
		VisualRegions          []VisualRegion    `json:"visual_regions"`
		Page                   int               `json:"page"`
		Status                 string            `json:"status"`
		Reason                 string            `json:"reason"`
		NativeBlocks           []json.RawMessage `json:"native_blocks"`
		UnresolvedVisualBlocks int               `json:"unresolved_visual_blocks"`
	} `json:"pages"`
}

func validateOCR(d *Document, spec Spec) bool {
	if spec.OCR == nil {
		return d.OCR == nil && d.Processor.Version == Processor
	}
	if spec.Format != "png" && spec.Format != "jpeg" && spec.Format != "pdf" {
		return false
	}
	if spec.OCR.Validate() != nil || d.OCR == nil || d.Processor.Version != ocr.Processor || d.OCR.Identity != *spec.OCR || d.OCR.ClientVersion != ocr.ClientVersion || d.OCR.Policy != "selective-pages-v1" || d.OCR.Pages == nil || len(d.OCR.Pages) > 300 {
		return false
	}
	seen := map[int]bool{}
	unread := map[int]int{}
	readable := map[int]bool{}
	marked := map[int]bool{}
	for _, b := range d.Blocks {
		if strings.TrimSpace(b.Text) != "" || readableTable(b.Cells) {
			readable[b.Location.Page] = true
		}
		if b.RecognitionStatus == "" {
			continue
		}
		if b.RecognitionStatus != "unread" || b.Type != "image" || strings.TrimSpace(b.Text) != "" ||
			(b.Label != "Image" && b.Label != "Figure" && b.Label != "Diagram") || b.Location.Page < 1 || b.Location.Page > len(d.Pages) {
			return false
		}
		unread[b.Location.Page]++
	}
	for _, issue := range d.Quality.Issues {
		if issue.Code == "ocr_visual_content_unread" && issue.RequiresOCR {
			marked[issue.Location.Page] = true
		}
	}
	unresolved := false
	for _, p := range d.OCR.Pages {
		if p.Page < 1 || p.Page > len(d.Pages) || seen[p.Page] || p.NativeBlocks == nil {
			return false
		}
		seen[p.Page] = true
		switch p.Status {
		case "completed":
			if p.Reason != "" || unread[p.Page] != 0 || p.UnresolvedVisualBlocks != 0 {
				return false
			}
		case "partial":
			if p.Reason != "visual_content_unread" || unread[p.Page] == 0 || !readable[p.Page] || !marked[p.Page] ||
				p.UnresolvedVisualBlocks != unread[p.Page] {
				return false
			}
			unresolved = true
		case "failed", "skipped":
			if p.Reason == "" || unread[p.Page] != 0 || p.UnresolvedVisualBlocks != 0 {
				return false
			}
			unresolved = true
		default:
			return false
		}
	}
	for page := range unread {
		if !seen[page] {
			return false
		}
	}
	// Model output always requires review; incomplete OCR cannot be hidden.
	if len(d.OCR.Pages) > 0 && d.Quality.State == "ready" {
		return false
	}
	if unresolved && d.Quality.State != "needs_ocr" {
		return false
	}
	return true
}

func ExpectedSpec(document, sha, format, rules, image, rawOCR string) (Spec, error) {
	identity, e := ocr.Decode(rawOCR)
	if e != nil {
		return Spec{}, fmt.Errorf("stored OCR identity invalid")
	}
	return Spec{DocumentID: document, SHA256: sha, Format: format, RulesVersion: rules, ImageID: image, OCR: identity}, nil
}

func readableTable(raw json.RawMessage) bool {
	var cells []struct {
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &cells) != nil {
		return false
	}
	for _, c := range cells {
		if strings.TrimSpace(c.Text) != "" {
			return true
		}
	}
	return false
}
