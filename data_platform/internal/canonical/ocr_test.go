package canonical

import (
	"data-platform/internal/ocr"
	"encoding/json"
	"strings"
	"testing"
)

func TestOCRCanonicalCannotLosePinnedModelOrHideIncompletePages(t *testing.T) {
	spec, body := validFixture()
	spec.Format = "png"
	body["source"].(map[string]string)["format"] = "png"
	id := "11111111-1111-4111-8111-111111111111"
	spec.OCR = &ocr.Identity{DeploymentID: id, ModelVersionID: "22222222-2222-4222-8222-222222222222", RuntimeImageID: "sha256:" + strings.Repeat("c", 64), RuntimeContainerID: strings.Repeat("d", 64), Endpoint: "http://model-serving-" + id + ":8000/v1", Model: "deployment-" + id, Profile: ocr.Profile, ManifestSHA256: strings.Repeat("e", 64)}
	body["processor"].(map[string]string)["version"] = ocr.Processor
	body["pages"] = []map[string]any{{"number": 1}}
	pages := []map[string]any{{"page": 1, "status": "completed", "reason": "", "native_blocks": []any{}}}
	result := map[string]any{"identity": spec.OCR, "client_version": ocr.ClientVersion, "policy": "selective-pages-v1", "pages": pages}
	body["ocr"] = result
	body["quality"] = map[string]any{"state": "needs_review", "issues": []map[string]any{{"code": "ocr_output_requires_review", "requires_ocr": false}}}
	valid, _ := json.Marshal(body)
	if _, e := Validate(valid, spec); e != nil {
		t.Fatal(e)
	}
	without := spec
	without.OCR = nil
	if _, e := Validate(valid, without); e == nil {
		t.Fatal("unrequested OCR accepted")
	}
	changed := *spec.OCR
	changed.RuntimeImageID = "sha256:" + strings.Repeat("f", 64)
	result["identity"] = &changed
	raw, _ := json.Marshal(body)
	if _, e := Validate(raw, spec); e == nil {
		t.Fatal("wrong OCR runtime accepted")
	}
	result["identity"] = spec.OCR
	pages[0]["status"] = "failed"
	pages[0]["reason"] = "timeout"
	raw, _ = json.Marshal(body)
	if _, e := Validate(raw, spec); e == nil {
		t.Fatal("failed page hidden as review")
	}
	body["quality"] = map[string]any{"state": "needs_ocr", "issues": []map[string]any{{"code": "ocr_timeout", "requires_ocr": true}}}
	raw, _ = json.Marshal(body)
	if _, e := Validate(raw, spec); e != nil {
		t.Fatal("honest fallback rejected", e)
	}
}

func TestPartialOCRRequiresReadableContentAndMatchingUnresolvedVisual(t *testing.T) {
	fixture := func() (Spec, map[string]any) {
		spec, body := validFixture()
		spec.Format = "pdf"
		body["source"].(map[string]string)["format"] = "pdf"
		id := "11111111-1111-4111-8111-111111111111"
		spec.OCR = &ocr.Identity{DeploymentID: id, ModelVersionID: "22222222-2222-4222-8222-222222222222", RuntimeImageID: "sha256:" + strings.Repeat("c", 64), RuntimeContainerID: strings.Repeat("d", 64), Endpoint: "http://model-serving-" + id + ":8000/v1", Model: "deployment-" + id, Profile: ocr.Profile, ManifestSHA256: strings.Repeat("e", 64)}
		body["processor"].(map[string]string)["version"] = ocr.Processor
		body["pages"] = []map[string]any{{"number": 1}}
		body["blocks"] = []map[string]any{
			{"id": "b1", "order": 0, "type": "paragraph", "text": "read body", "location": map[string]int{"page": 1}},
			{"id": "b2", "order": 1, "type": "image", "label": "Diagram", "text": "", "recognition_status": "unread", "location": map[string]int{"page": 1}},
		}
		body["ocr"] = map[string]any{"identity": spec.OCR, "client_version": ocr.ClientVersion, "policy": "selective-pages-v1", "pages": []map[string]any{
			{"page": 1, "status": "partial", "reason": "visual_content_unread", "unresolved_visual_blocks": 1, "native_blocks": []any{}},
		}}
		body["quality"] = map[string]any{"state": "needs_ocr", "issues": []map[string]any{
			{"code": "ocr_visual_content_unread", "requires_ocr": true, "location": map[string]int{"page": 1}},
		}}
		return spec, body
	}
	spec, body := fixture()
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("honest partial rejected", err)
	}
	for _, name := range []string{"review", "completed", "missing_reason", "wrong_count", "no_text", "no_visual", "wrong_page", "missing_issue", "text_label"} {
		t.Run(name, func(t *testing.T) {
			spec, body := fixture()
			page := body["ocr"].(map[string]any)["pages"].([]map[string]any)[0]
			blocks := body["blocks"].([]map[string]any)
			switch name {
			case "review":
				body["quality"].(map[string]any)["state"] = "needs_review"
			case "completed":
				page["status"], page["reason"] = "completed", ""
			case "missing_reason":
				page["reason"] = ""
			case "wrong_count":
				page["unresolved_visual_blocks"] = 2
			case "no_text":
				blocks[0]["text"] = ""
			case "no_visual":
				delete(blocks[1], "recognition_status")
			case "wrong_page":
				blocks[1]["location"] = map[string]int{"page": 2}
			case "missing_issue":
				body["quality"].(map[string]any)["issues"] = []any{}
			case "text_label":
				blocks[1]["label"] = "Text"
			}
			raw, _ := json.Marshal(body)
			if _, err := Validate(raw, spec); err == nil {
				t.Fatal("partial quality hidden", name)
			}
		})
	}
}
