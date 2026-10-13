package canonical

import (
	"data-platform/internal/ocr"
	"encoding/json"
	"strings"
	"testing"
)

func tableFixture() (Spec, map[string]any) {
	spec, body := validFixture()
	spec.Format = "pdf"
	body["source"].(map[string]string)["format"] = "pdf"
	cells := []map[string]any{{"row": 0, "column": 0, "row_span": 1, "column_span": 1, "text": "금액 1,200"}}
	location := map[string]any{"page": 1, "bbox": []int{10, 10, 90, 90}, "coordinate_system": "pdf-points-top-left"}
	block := map[string]any{"id": "b1", "order": 0, "type": "table", "cells": cells, "location": location, "table_comparison_id": "t1"}
	native := map[string]any{"type": "table", "cells": cells, "location": location}
	metric := map[string]any{"valid": true, "rows": 1, "columns": 1, "nonempty_cells": 1, "issues": []string{}}
	row := map[string]any{"id": "t1", "page": 1, "bbox": []int{10, 10, 90, 90}, "coordinate_system": "pdf-points-top-left",
		"status": "needs_review", "selected": "parser", "reasons": []string{"ocr_not_requested"},
		"parser_candidates": []map[string]any{native}, "ocr_candidates": []map[string]any{},
		"parser_metrics": []map[string]any{metric}, "ocr_metrics": []map[string]any{},
		"result_block_ids": []string{"b1"}, "difference_count": 0, "differences": []any{}, "differences_truncated": false}
	body["blocks"] = []map[string]any{block}
	body["pages"] = []map[string]any{{"number": 1, "width": 100, "height": 100, "coordinate_system": "pdf-points-top-left"}}
	body["quality"] = map[string]any{"state": "needs_review", "issues": []map[string]any{{"code": "table_comparison_requires_review", "requires_ocr": false, "location": map[string]any{"page": 1, "table_id": "t1"}}}}
	body["table_comparison"] = map[string]any{"version": "table-comparison-v1", "policy": "conservative-v1", "tables": []map[string]any{row}}
	return spec, body
}
func TestTableComparisonBindsSelectionAndReviewToCanonical(t *testing.T) {
	spec, body := tableFixture()
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"changed_value", "wrong_block", "orphan", "hidden_review", "wrong_page", "wrong_status", "false_agreement", "missing_candidate", "missing_comparison", "metrics_lie", "truncated_lie"} {
		t.Run(name, func(t *testing.T) {
			spec, body := tableFixture()
			block := body["blocks"].([]map[string]any)[0]
			row := body["table_comparison"].(map[string]any)["tables"].([]map[string]any)[0]
			switch name {
			case "changed_value":
				block["cells"] = []map[string]any{{"row": 0, "column": 0, "text": "9900"}}
			case "wrong_block":
				row["result_block_ids"] = []string{"b2"}
			case "orphan":
				block["table_comparison_id"] = "t2"
			case "hidden_review":
				body["quality"] = map[string]any{"state": "ready", "issues": []any{}}
			case "wrong_page":
				row["page"] = 2
			case "wrong_status":
				row["status"] = "passed"
			case "false_agreement":
				row["status"] = "agreed"
			case "missing_candidate":
				row["parser_candidates"] = []any{}
			case "missing_comparison":
				delete(body, "table_comparison")
			case "metrics_lie":
				row["parser_metrics"].([]map[string]any)[0]["valid"] = false
			case "truncated_lie":
				row["difference_count"] = 120
			}
			raw, _ := json.Marshal(body)
			if _, err := Validate(raw, spec); err == nil {
				t.Fatal("inconsistent comparison accepted", name)
			}
		})
	}
}

func TestTableAgreementRequiresEqualCellsAndCompletedPinnedOCR(t *testing.T) {
	spec, body := tableFixture()
	id := "11111111-1111-4111-8111-111111111111"
	spec.OCR = &ocr.Identity{DeploymentID: id, ModelVersionID: "22222222-2222-4222-8222-222222222222", RuntimeImageID: "sha256:" + strings.Repeat("c", 64), RuntimeContainerID: strings.Repeat("d", 64), Endpoint: "http://model-serving-" + id + ":8000/v1", Model: "deployment-" + id, Profile: ocr.Profile, ManifestSHA256: strings.Repeat("e", 64)}
	body["processor"].(map[string]string)["version"] = ocr.Processor
	body["ocr"] = map[string]any{"identity": spec.OCR, "client_version": ocr.ClientVersion, "policy": "selective-pages-v1", "pages": []map[string]any{{"page": 1, "status": "completed", "reason": "", "native_blocks": []any{}}}}
	row := body["table_comparison"].(map[string]any)["tables"].([]map[string]any)[0]
	row["status"], row["reasons"] = "agreed", []string{"agreement"}
	native := row["parser_candidates"].([]map[string]any)[0]
	recognized := map[string]any{"type": "table", "location": native["location"], "cells": []map[string]any{{"row": 0, "column": 0, "text": "금 액\n1,200"}}}
	row["ocr_candidates"] = []map[string]any{recognized}
	row["ocr_metrics"] = row["parser_metrics"]
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("normalized agreement rejected", err)
	}
	// The old policy stays readable; new results must honor OCR preference.
	body["table_comparison"].(map[string]any)["policy"] = "ocr-priority-v1"
	row["selected"] = "ocr"
	block := body["blocks"].([]map[string]any)[0]
	block["cells"] = recognized["cells"]
	body["processing"] = map[string]string{"version": "refinement-processing-v1", "mode": "ocr_priority"}
	raw, _ = json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("OCR priority rejected", err)
	}
	row["selected"] = "parser"
	block["cells"] = native["cells"]
	raw, _ = json.Marshal(body)
	if _, err := Validate(raw, spec); err == nil {
		t.Fatal("new policy selected parser over readable OCR")
	}
	row["selected"] = "ocr"
	block["cells"] = recognized["cells"]
	recognized["cells"] = []map[string]any{{"row": 0, "column": 0, "text": "금액 9,900"}}
	raw, _ = json.Marshal(body)
	if _, err := Validate(raw, spec); err == nil {
		t.Fatal("false numerical agreement accepted")
	}
}

func TestTableGridBoundsMergedSpansAndUnreadableText(t *testing.T) {
	for _, raw := range []string{`[{"row":0,"column":0,"text":""}]`, `[{"row":0,"column":0,"row_span":999999999,"text":"A"}]`, `[{"row":0,"column":0,"text":"(cid:32)"}]`, `[{"row":0,"column":0,"text":"A"},{"row":0,"column":0,"text":"B"}]`} {
		if _, valid := tableGrid([]byte(raw)); valid {
			t.Fatal("invalid grid accepted")
		}
	}
	if _, valid := tableGrid([]byte(`[{"row":0,"column":0,"column_span":2,"text":"A"},{"row":1,"column":0,"text":"B"},{"row":1,"column":1,"text":"C"}]`)); !valid {
		t.Fatal("valid merged grid rejected")
	}
}

func TestProcessingModeCannotClaimUnrequestedOCR(t *testing.T) {
	spec, body := validFixture()
	mode := map[string]string{"version": "refinement-processing-v1", "mode": "parser_only"}
	body["processing"] = mode
	raw, _ := json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal(err)
	}
	for _, changed := range []map[string]string{
		{"version": "refinement-processing-v1", "mode": "ocr_priority"},
		{"version": "unknown", "mode": "parser_only"},
		{"version": "refinement-processing-v1", "mode": "unknown"},
	} {
		body["processing"] = changed
		raw, _ = json.Marshal(body)
		if _, err := Validate(raw, spec); err == nil {
			t.Fatal("incorrect processing receipt accepted")
		}
	}
	delete(body, "processing")
	raw, _ = json.Marshal(body)
	if _, err := Validate(raw, spec); err != nil {
		t.Fatal("legacy processing result rejected", err)
	}
}
