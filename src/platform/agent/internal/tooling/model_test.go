package tooling

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestStrictJSONAndSchemas(t *testing.T) {
	for _, raw := range []string{`{"text":"a","text":"b"}`, `{"text":null}`, `{"text":"a"} {}`, strings.Repeat("[", 34) + "0" + strings.Repeat("]", 34), `{"text":"a","url":"http://other"}`} {
		if ValidateArguments(ToolVersion{Kind: "diagnostic_echo_v1"}, []byte(raw)) == nil {
			t.Fatalf("accepted invalid arguments %s", raw)
		}
	}
	var out map[string]any
	if StrictJSON([]byte("{\"text\":\"\xff\"}"), &out) == nil {
		t.Fatal("invalid utf8")
	}
	if ValidateArguments(ToolVersion{Kind: "diagnostic_echo_v1"}, JSON(map[string]string{"text": "한국어"})) != nil {
		t.Fatal("valid input rejected")
	}
	first, _ := Canonical([]byte(`{"b":2,"a":1}`))
	second, _ := Canonical([]byte(`{ "a": 1, "b": 2 }`))
	if string(first) != string(second) {
		t.Fatal("key order changed identity")
	}
	in, outSchema, err := Schemas("diagnostic_echo_v1")
	if err != nil || !json.Valid(in) || !json.Valid(outSchema) {
		t.Fatal("schemas")
	}
	if _, _, err = Schemas("python"); err == nil {
		t.Fatal("arbitrary adapter accepted")
	}
	if ValidateOutput(ToolVersion{Kind: "diagnostic_echo_v1"}, []byte(`{"value":"wrong"}`)) == nil {
		t.Fatal("invalid output accepted")
	}
}
func TestConnectionReferencesAndUnsafeURLs(t *testing.T) {
	for _, base := range []string{"file:///etc/passwd", "http://localhost/api", "http://127.0.0.1", "http://[::1]", "http://169.254.169.254", "http://user:secret@service", "http://service?token=secret", "http://service/#fragment"} {
		if ValidateConnection(ConnectionSpec{BaseURL: base, AuthType: "none"}) == nil {
			t.Fatal(base)
		}
	}
	valid := ConnectionSpec{BaseURL: "http://model-gateway:8000", AuthType: "bearer", CredentialRef: "model_api", CredentialRevision: "v1"}
	if ValidateConnection(valid) != nil {
		t.Fatal("valid explicit connection rejected")
	}
	valid.CredentialRef = "../api-token"
	if ValidateConnection(valid) == nil {
		t.Fatal("path accepted as reference")
	}
}
