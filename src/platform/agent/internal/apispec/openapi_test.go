package apispec

import (
	"encoding/json"
	"strings"
	"testing"
)

func fixture() map[string]any {
	v, e := JSON([]byte(`{"openapi":"3.0.3","info":{"title":"test","version":"1"},"paths":{"/items/{id}":{"get":{"parameters":[{"in":"path","name":"id","required":true,"schema":{"type":"string"}},{"in":"query","name":"id","schema":{"type":"array","items":{"type":"integer"}}},{"in":"header","name":"X-Label","schema":{"type":"string"}}],"responses":{"200":{"description":"ok","content":{"application/json":{"schema":{"$ref":"#/components/schemas/Result"}}}}}}}},"components":{"schemas":{"Result":{"type":"object","properties":{"name":{"type":"string","nullable":true}},"required":["name"],"additionalProperties":false}}}}`), MaxDocument)
	if e != nil {
		panic(e)
	}
	return v.(map[string]any)
}
func analyze(t *testing.T, doc map[string]any) Operation {
	t.Helper()
	raw, _ := json.Marshal(doc)
	a, e := Parse(raw, "json")
	if e != nil {
		t.Fatal(e)
	}
	return a.Operations[0]
}
func getOp(doc map[string]any) map[string]any {
	return doc["paths"].(map[string]any)["/items/{id}"].(map[string]any)["get"].(map[string]any)
}
func TestOpenAPINormalizationAndValidation(t *testing.T) {
	op := analyze(t, fixture())
	if !op.Supported {
		t.Fatal(op.Problems)
	}
	for _, raw := range []string{`{"path":{"id":"가"},"query":{"id":[1,2]},"header":{"X-Label":"test"}}`, `{"path":{"id":"x"}}`} {
		v, _ := JSON([]byte(raw), 1024)
		if Validate(op.InputSchema, v) != nil {
			t.Fatal(raw)
		}
	}
	for _, raw := range []string{`{"path":{"id":null}}`, `{"path":{"id":"x"},"query":{"id":[true]}}`, `{"path":{"id":"x","url":"http://other"}}`, `{}`} {
		v, _ := JSON([]byte(raw), 1024)
		if Validate(op.InputSchema, v) == nil {
			t.Fatal("invalid accepted", raw)
		}
	}
	v, _ := JSON([]byte(`{"name":null}`), 1024)
	if Validate(op.Responses["200"], v) != nil {
		t.Fatal("nullable lost")
	}
	d := fixture()
	d["openapi"] = "3.1.1"
	s := d["components"].(map[string]any)["schemas"].(map[string]any)["Result"].(map[string]any)["properties"].(map[string]any)["name"].(map[string]any)
	delete(s, "nullable")
	s["type"] = []any{"string", "null"}
	if !analyze(t, d).Supported {
		t.Fatal("3.1 nullable")
	}
}
func TestOpenAPIUnsupportedIsExplicit(t *testing.T) {
	cases := map[string]func(map[string]any){
		"remote-ref": func(d map[string]any) {
			d["components"].(map[string]any)["schemas"].(map[string]any)["Result"] = map[string]any{"$ref": "https://example.test/schema"}
		},
		"cycle": func(d map[string]any) {
			d["components"].(map[string]any)["schemas"].(map[string]any)["Result"] = map[string]any{"$ref": "#/components/schemas/Result"}
		},
		"composition": func(d map[string]any) {
			d["components"].(map[string]any)["schemas"].(map[string]any)["Result"] = map[string]any{"oneOf": []any{map[string]any{"type": "string"}}}
		},
		"reserved-header": func(d map[string]any) { getOp(d)["parameters"].([]any)[2].(map[string]any)["name"] = "Authorization" },
		"deep-object":     func(d map[string]any) { getOp(d)["parameters"].([]any)[1].(map[string]any)["style"] = "deepObject" },
		"async": func(d map[string]any) {
			getOp(d)["responses"].(map[string]any)["202"] = map[string]any{"description": "accepted"}
		},
		"unknown-keyword": func(d map[string]any) {
			d["components"].(map[string]any)["schemas"].(map[string]any)["Result"].(map[string]any)["unevaluatedProperties"] = false
		},
		"cookie":          func(d map[string]any) { getOp(d)["parameters"].([]any)[1].(map[string]any)["in"] = "cookie" },
		"duplicate-param": func(d map[string]any) { p := getOp(d)["parameters"].([]any); getOp(d)["parameters"] = append(p, p[0]) },
		"callback":        func(d map[string]any) { getOp(d)["callbacks"] = map[string]any{} },
		"security-null":   func(d map[string]any) { getOp(d)["security"] = nil },
		"server-userinfo": func(d map[string]any) { d["servers"] = []any{map[string]any{"url": "https://user:pass@example.test"}} },
	}
	for name, mutate := range cases {
		t.Run(name, func(t *testing.T) {
			d := fixture()
			mutate(d)
			op := analyze(t, d)
			if op.Supported || len(op.Problems) == 0 || op.Problems[0].Pointer == "" {
				t.Fatal("unsupported silently accepted", op)
			}
		})
	}
}
func TestOpenAPISecurityAndOverrides(t *testing.T) {
	d := fixture()
	d["components"].(map[string]any)["securitySchemes"] = map[string]any{"token": map[string]any{"type": "http", "scheme": "bearer"}, "key": map[string]any{"type": "apiKey", "in": "query", "name": "api_key"}}
	d["security"] = []any{map[string]any{"token": []any{}}, map[string]any{"key": []any{}}}
	op := analyze(t, d)
	if !op.Supported || len(op.AuthOptions) != 2 {
		t.Fatal(op)
	}
	getOp(d)["security"] = []any{}
	op = analyze(t, d)
	if len(op.AuthOptions) != 1 || op.AuthOptions[0].Type != "none" {
		t.Fatal(op.AuthOptions)
	}
	getOp(d)["security"] = []any{map[string]any{"token": []any{}, "key": []any{}}}
	if analyze(t, d).Supported {
		t.Fatal("AND accepted")
	}
}
func TestOpenAPIParserBoundaries(t *testing.T) {
	for _, raw := range []string{`{"openapi":"3.0.3","openapi":"3.1.1"}`, `{} {}`, `{"x":1e100000}`} {
		if _, e := JSON([]byte(raw), MaxDocument); e == nil {
			t.Fatal("JSON accepted", raw)
		}
	}
	for _, raw := range []string{"x: 1\nx: 2", "x: &a [1]\ny: *a", "---\na: 1\n---\nb: 2", "x: !!binary YQ==", "x: .inf"} {
		if _, e := YAML([]byte(raw)); e == nil {
			t.Fatal("YAML accepted", raw)
		}
	}
	yaml := `openapi: 3.0.3
info: {title: test, version: "1"}
paths:
  /ping:
    get:
      responses:
        204: {description: done}
`
	a, e := Parse([]byte(yaml), "yaml")
	if e != nil || !a.Operations[0].Supported {
		t.Fatal(a, e)
	}
	if _, e = Parse([]byte(strings.Repeat(" ", MaxDocument+1)), "json"); e == nil {
		t.Fatal("oversize")
	}
}
func TestOpenAPINumericAndObjectSchema(t *testing.T) {
	c := &compiler{root: map[string]any{}, v30: false}
	v, _ := JSON([]byte(`{"type":"object","properties":{"n":{"type":"integer","minimum":-2,"maximum":9223372036854775807,"format":"int64"},"s":{"type":"string","minLength":2,"pattern":"^[가-힣]+$"}},"required":["n","s"],"additionalProperties":false}`), MaxDocument)
	schema, e := c.schema(v, "/schema", nil, 0)
	if e != nil {
		t.Fatal(e)
	}
	for _, raw := range []string{`{"n":9223372036854775807,"s":"가나"}`, `{"n":1.0,"s":"다라"}`} {
		v, _ := JSON([]byte(raw), 1024)
		if Validate(schema, v) != nil {
			t.Fatal(raw)
		}
	}
	for _, raw := range []string{`{"n":9223372036854775808,"s":"가나"}`, `{"n":true,"s":"가나"}`, `{"n":0,"s":"a가"}`, `{"n":0,"s":"가"}`} {
		v, _ := JSON([]byte(raw), 1024)
		if Validate(schema, v) == nil {
			t.Fatal(raw)
		}
	}
}
