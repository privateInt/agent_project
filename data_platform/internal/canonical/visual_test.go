package canonical

import (
	"data-platform/internal/ocr"
	"encoding/json"
	"strings"
	"testing"
)

func visualFixture() (Spec, map[string]any) {
	spec, body := validFixture()
	spec.Format = "png"
	body["source"].(map[string]string)["format"] = "png"
	id := "11111111-1111-4111-8111-111111111111"
	spec.OCR = &ocr.Identity{DeploymentID: id, ModelVersionID: "22222222-2222-4222-8222-222222222222", RuntimeImageID: "sha256:" + strings.Repeat("c", 64), RuntimeContainerID: strings.Repeat("d", 64), Endpoint: "http://model-serving-" + id + ":8000/v1", Model: "deployment-" + id, Profile: ocr.Profile, ManifestSHA256: strings.Repeat("e", 64)}
	body["processor"].(map[string]string)["version"] = ocr.Processor
	body["pages"] = []map[string]any{{"number": 1, "width": 100, "height": 200, "coordinate_system": "image-pixels-top-left"}}
	loc := map[string]any{"page": 1, "bbox": []float64{10, 60, 80, 140}, "coordinate_system": "image-pixels-top-left"}
	region := map[string]any{"id": "v1", "source_label": "Diagram", "source_location": loc, "location": loc, "status": "completed", "reason": "", "result_blocks": 1}
	body["blocks"] = []map[string]any{{"id": "b1", "order": 0, "type": "paragraph", "text": "Read image text", "visual_region_id": "v1", "location": loc, "model_bbox": []float64{0, 0, 1000, 1000}, "model_coordinate_system": "normalized-0-1000-crop-top-left"}}
	body["ocr"] = map[string]any{"identity": spec.OCR, "client_version": ocr.ClientVersion, "policy": "selective-pages-v1", "pages": []map[string]any{{"page": 1, "status": "completed", "reason": "", "native_blocks": []any{}, "visual_regions": []map[string]any{region}}}}
	body["quality"] = map[string]any{"state": "needs_review", "issues": []map[string]any{{"code": "ocr_output_requires_review", "requires_ocr": false}}}
	return spec, body
}

func TestVisualCropRequiresTraceableMappedBlocksAndHonestStatus(t *testing.T) {

	spec, body := visualFixture()
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("valid visual crop rejected", err)
	}
	for _, name := range []string{"unknown_region", "wrong_count", "wrong_page", "wrong_bbox", "wrong_model_bbox", "unread", "empty", "failed", "lost_reference", "duplicate_region", "wrong_crop_origin", "unrequested"} {
		t.Run(name, func(t *testing.T) {
			spec, body := visualFixture()
			block := body["blocks"].([]map[string]any)[0]
			page := body["ocr"].(map[string]any)["pages"].([]map[string]any)[0]
			region := page["visual_regions"].([]map[string]any)[0]
			switch name {
			case "unknown_region":
				block["visual_region_id"] = "v2"
			case "wrong_count":
				region["result_blocks"] = 2
			case "wrong_page":
				block["location"] = map[string]any{"page": 2, "bbox": []float64{10, 60, 80, 140}, "coordinate_system": "image-pixels-top-left"}
			case "wrong_bbox":
				block["location"] = map[string]any{"page": 1, "bbox": []float64{0, 0, 100, 200}, "coordinate_system": "image-pixels-top-left"}
			case "wrong_model_bbox":
				block["model_bbox"] = []float64{0, 0, 1001, 1000}
			case "unread":
				block["recognition_status"] = "unread"
			case "empty":
				block["text"] = ""
			case "failed":
				region["status"] = "failed"
				region["reason"] = "timeout"
			case "lost_reference":
				delete(block, "visual_region_id")
			case "duplicate_region":
				page["visual_regions"] = []any{region, region}
			case "wrong_crop_origin":
				region["location"] = map[string]any{"page": 1, "bbox": []float64{20, 60, 80, 140}, "coordinate_system": "image-pixels-top-left"}
			case "unrequested":
				spec.OCR = nil
				delete(body, "ocr")
				body["processor"].(map[string]string)["version"] = Processor
			}
			raw, _ := json.Marshal(body)
			if _, err := Validate(raw, spec); err == nil {
				t.Fatal("invalid visual provenance accepted", name)
			}
		})
	}
}

func TestPartialVisualCropKeepsReadableChildrenAndUnresolvedSource(t *testing.T) {
	fixture := func() (Spec, map[string]any) {
		spec, body := visualFixture()
		page := body["ocr"].(map[string]any)["pages"].([]map[string]any)[0]
		region := page["visual_regions"].([]map[string]any)[0]
		region["status"], region["reason"] = "partial", "visual_text_unread"
		page["status"], page["reason"], page["unresolved_visual_blocks"] = "partial", "visual_content_unread", 1
		loc := region["source_location"]
		body["blocks"] = append(body["blocks"].([]map[string]any), map[string]any{
			"id": "b2", "order": 1, "type": "image", "label": "Diagram", "text": "",
			"recognition_status": "unread", "location": loc})
		body["quality"] = map[string]any{"state": "needs_ocr", "issues": []map[string]any{
			{"code": "ocr_output_requires_review", "requires_ocr": false},
			{"code": "ocr_visual_content_unread", "requires_ocr": true, "location": loc}}}
		return spec, body
	}
	spec, body := fixture()
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("valid partial crop rejected", err)
	}
	for _, name := range []string{"source_missing", "wrong_unread_box", "zero_children", "wrong_count", "unknown_child", "failed_with_text", "completed_with_unread", "wrong_reason", "page_completed", "review_only", "two_markers"} {
		t.Run(name, func(t *testing.T) {
			spec, body := fixture()
			page := body["ocr"].(map[string]any)["pages"].([]map[string]any)[0]
			region := page["visual_regions"].([]map[string]any)[0]
			blocks := body["blocks"].([]map[string]any)
			switch name {
			case "source_missing":
				body["blocks"] = blocks[:1]
			case "wrong_unread_box":
				blocks[1]["location"] = map[string]any{"page": 1, "coordinate_system": "image-pixels-top-left", "bbox": []float64{11, 60, 80, 140}}
			case "zero_children":
				region["result_blocks"] = 0
			case "wrong_count":
				region["result_blocks"] = 2
			case "unknown_child":
				blocks[0]["visual_region_id"] = "v2"
			case "failed_with_text":
				region["status"] = "failed"
			case "completed_with_unread":
				region["status"], region["reason"] = "completed", ""
			case "wrong_reason":
				region["reason"] = "timeout"
			case "page_completed":
				page["status"], page["reason"] = "completed", ""
			case "review_only":
				body["quality"].(map[string]any)["state"] = "needs_review"
			case "two_markers":
				body["blocks"] = append(blocks, map[string]any{"id": "b3", "order": 2, "type": "image", "label": "Diagram", "recognition_status": "unread", "location": region["source_location"]})
				page["unresolved_visual_blocks"] = 2
			}
			raw, _ := json.Marshal(body)
			if _, err := Validate(raw, spec); err == nil {
				t.Fatal("false partial/completed result accepted", name)
			}
		})
	}
}
