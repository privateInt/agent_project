package canonical

import (
	collectionv1 "data-platform/gen/collection/v1"
	"encoding/json"
	"strings"
	"testing"
)

func validFixture() (Spec, map[string]any) {
	s := Spec{DocumentID: "11111111-1111-4111-8111-111111111111", SHA256: strings.Repeat("a", 64), Format: "text", RulesVersion: Rules, ImageID: "sha256:" + strings.Repeat("b", 64)}
	return s, map[string]any{"schema_version": Schema, "source": map[string]string{"document_id": s.DocumentID, "sha256": s.SHA256, "format": s.Format}, "processor": map[string]string{"version": Processor, "rules_version": Rules, "image_id": s.ImageID}, "blocks": []map[string]any{{"id": "b1", "order": 0, "type": "text", "text": "hello"}}, "pages": []any{}, "quality": map[string]any{"state": "ready", "issues": []any{}}, "cleaning": map[string]string{"version": Rules}}
}
func TestCanonicalRejectsFalseQualityDuplicateKeysAndWrongProvenance(t *testing.T) {
	spec, obj := validFixture()
	raw, _ := json.Marshal(obj)
	if _, e := Validate(raw, spec); e != nil {
		t.Fatal(e)
	}
	duplicate := strings.Replace(string(raw), `"schema_version":"`+Schema+`"`, `"schema_version":"fake","schema_version":"`+Schema+`"`, 1)
	if _, e := Validate([]byte(duplicate), spec); e == nil {
		t.Fatal("ambiguous JSON accepted")
	}
	bad := spec
	bad.DocumentID = "22222222-2222-4222-8222-222222222222"
	if _, e := Validate(raw, bad); e == nil {
		t.Fatal("wrong source accepted")
	}
	obj["quality"] = map[string]any{"state": "ready", "issues": []map[string]any{{"code": "missing_image", "requires_ocr": true}}}
	raw, _ = json.Marshal(obj)
	if _, e := Validate(raw, spec); e == nil {
		t.Fatal("OCR requirement hidden as ready")
	}
}
func TestCanonicalSourceFallbackAndNamespace(t *testing.T) {
	f, e := Format(&collectionv1.SourceDocument{ContentType: "text/html; charset=utf-8", Format: "unknown"})
	if e != nil || f != "html" {
		t.Fatal(f, e)
	}
	if _, e = Format(&collectionv1.SourceDocument{ContentType: "application/octet-stream"}); e == nil {
		t.Fatal("unknown binary accepted")
	}
	if _, e = Key("../escape", strings.Repeat("a", 64)); e == nil {
		t.Fatal("invalid path accepted")
	}
}
