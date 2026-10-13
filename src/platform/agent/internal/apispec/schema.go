// Package apispec compiles the explicitly supported OpenAPI subset. It never
// fetches references or executes requests. Compiled schemas are JSON Schema.
package apispec

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"math/big"
	"reflect"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"
)

const MaxDocument = 5 << 20
const MaxAnalysis = 1 << 20
const MaxOperation = 64 << 10

type Problem struct {
	Pointer string `json:"pointer"`
	Reason  string `json:"reason"`
}

func (p *Problem) Error() string          { return p.Pointer + ": " + p.Reason }
func problem(path, reason string) error   { return &Problem{path, reason} }
func object(v any) (map[string]any, bool) { m, ok := v.(map[string]any); return m, ok }
func text(v any) string                   { s, _ := v.(string); return s }
func truth(v any) bool                    { b, _ := v.(bool); return b }
func pointer(s string) string             { return strings.ReplaceAll(strings.ReplaceAll(s, "~", "~0"), "/", "~1") }
func keys(m map[string]any, allowed, path string) error {
	for k := range m {
		if strings.HasPrefix(k, "x-") {
			continue
		}
		if !strings.Contains(" "+allowed+" ", " "+k+" ") {
			return problem(path+"/"+pointer(k), "unsupported field")
		}
	}
	return nil
}

// JSON rejects duplicate keys and non-finite/invalid numbers before unmarshalling.
func JSON(raw []byte, limit int) (any, error) {
	if len(raw) > limit || !utf8.Valid(raw) {
		return nil, problem("/", "invalid UTF-8 or size limit")
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	var walk func(int) (any, error)
	nodes := 0
	walk = func(depth int) (any, error) {
		nodes++
		if depth > 48 || nodes > 100000 {
			return nil, problem("/", "document depth or node limit")
		}
		t, e := d.Token()
		if e != nil {
			return nil, problem("/", "invalid JSON")
		}
		if delim, ok := t.(json.Delim); ok {
			if delim == '{' {
				m := map[string]any{}
				for d.More() {
					k, e := d.Token()
					if e != nil {
						return nil, e
					}
					key, ok := k.(string)
					if !ok {
						return nil, problem("/", "invalid key")
					}
					if _, exists := m[key]; exists {
						return nil, problem("/", "duplicate key")
					}
					v, e := walk(depth + 1)
					if e != nil {
						return nil, e
					}
					m[key] = v
				}
				_, e = d.Token()
				return m, e
			}
			if delim == '[' {
				a := []any{}
				for d.More() {
					v, e := walk(depth + 1)
					if e != nil {
						return nil, e
					}
					a = append(a, v)
				}
				_, e = d.Token()
				return a, e
			}
			return nil, problem("/", "invalid JSON")
		}
		if _, ok := t.(json.Number); ok {
			if _, valid := number(t); !valid {
				return nil, problem("/", "numeric range limit")
			}
		}
		return t, nil
	}
	v, e := walk(0)
	if e != nil {
		return nil, e
	}
	if _, e = d.Token(); e != io.EOF {
		return nil, problem("/", "trailing document")
	}
	return v, nil
}

type compiler struct {
	root  map[string]any
	v30   bool
	nodes int
}

func (c *compiler) deref(v any, path string, seen map[string]bool) (any, map[string]bool, error) {
	m, ok := object(v)
	if !ok {
		return v, seen, nil
	}
	ref, has := m["$ref"]
	if !has {
		return v, seen, nil
	}
	r, ok := ref.(string)
	if !ok || !strings.HasPrefix(r, "#/") {
		return nil, seen, problem(path+"/$ref", "only local JSON pointers are supported")
	}
	if len(m) != 1 {
		return nil, seen, problem(path, "$ref siblings are not supported")
	}
	if seen[r] {
		return nil, seen, problem(path, "cyclic reference")
	}
	next := map[string]bool{}
	for k, b := range seen {
		next[k] = b
	}
	next[r] = true
	if len(next) > 24 {
		return nil, next, problem(path, "reference depth limit")
	}
	var target any = c.root
	for _, part := range strings.Split(r[2:], "/") {
		part = strings.ReplaceAll(strings.ReplaceAll(part, "~1", "/"), "~0", "~")
		obj, ok := object(target)
		if !ok {
			return nil, next, problem(path, "unresolved reference")
		}
		target, ok = obj[part]
		if !ok {
			return nil, next, problem(path, "unresolved reference")
		}
	}
	return c.deref(target, path, next)
}
func (c *compiler) schema(v any, path string, seen map[string]bool, depth int) (any, error) {
	c.nodes++
	if depth > 20 || c.nodes > 4000 {
		return nil, problem(path, "schema expansion limit")
	}
	resolved, refs, e := c.deref(v, path, seen)
	if e != nil {
		return nil, e
	}
	if b, ok := resolved.(bool); ok && !c.v30 {
		return b, nil
	}
	m, ok := object(resolved)
	if !ok {
		return nil, problem(path, "schema object required")
	}
	if e = keys(m, "type properties required additionalProperties items enum const minimum maximum exclusiveMinimum exclusiveMaximum multipleOf minLength maxLength pattern format minItems maxItems uniqueItems minProperties maxProperties nullable title description default example examples deprecated readOnly writeOnly", path); e != nil {
		return nil, e
	}
	if truth(m["readOnly"]) || truth(m["writeOnly"]) {
		return nil, problem(path, "readOnly/writeOnly direction-dependent schemas are not supported")
	}
	out := map[string]any{}
	for _, k := range []string{"deprecated", "readOnly", "writeOnly"} {
		if value, has := m[k]; has {
			if _, ok := value.(bool); !ok {
				return nil, problem(path+"/"+k, "boolean required")
			}
		}
	}
	for k, val := range m {
		if strings.HasPrefix(k, "x-") {
			continue
		}
		switch k {
		case "properties":
			props, ok := object(val)
			if !ok {
				return nil, problem(path+"/properties", "object required")
			}
			newProps := map[string]any{}
			for name, p := range props {
				n, e := c.schema(p, path+"/properties/"+pointer(name), refs, depth+1)
				if e != nil {
					return nil, e
				}
				newProps[name] = n
			}
			out[k] = newProps
		case "items", "additionalProperties":
			if b, ok := val.(bool); ok && k == "additionalProperties" {
				out[k] = b
				continue
			}
			n, e := c.schema(val, path+"/"+k, refs, depth+1)
			if e != nil {
				return nil, e
			}
			out[k] = n
		case "type":
			types := []string{}
			if s, ok := val.(string); ok {
				types = []string{s}
			} else if a, ok := val.([]any); ok && !c.v30 {
				for _, v := range a {
					types = append(types, text(v))
				}
			} else {
				return nil, problem(path+"/type", "invalid type")
			}
			seenType := map[string]bool{}
			for _, s := range types {
				if !strings.Contains(" object array string integer number boolean null ", " "+s+" ") || s == "" || seenType[s] || c.v30 && s == "null" {
					return nil, problem(path+"/type", "invalid or duplicate type")
				}
				seenType[s] = true
			}
			if len(types) < 1 || len(types) > 2 || len(types) == 2 && !seenType["null"] {
				return nil, problem(path+"/type", "only one type with optional null is supported")
			}
			out[k] = val
		case "required":
			a, ok := val.([]any)
			if !ok {
				return nil, problem(path+"/required", "array required")
			}
			seen := map[string]bool{}
			for _, v := range a {
				s, ok := v.(string)
				if !ok || seen[s] {
					return nil, problem(path+"/required", "unique string keys required")
				}
				seen[s] = true
			}
			out[k] = val
		case "enum":
			a, ok := val.([]any)
			if !ok || len(a) == 0 || len(a) > 100 {
				return nil, problem(path+"/enum", "enum must contain 1..100 entries")
			}
			out[k] = val
		case "const":
			if c.v30 {
				return nil, problem(path+"/const", "const requires OpenAPI 3.1")
			}
			out[k] = val
		case "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf":
			if c.v30 && (k == "exclusiveMinimum" || k == "exclusiveMaximum") {
				if _, ok := val.(bool); !ok {
					return nil, problem(path+"/"+k, "boolean required in 3.0")
				}
				continue
			}
			n, ok := number(val)
			if !ok || k == "multipleOf" && n.Sign() <= 0 {
				return nil, problem(path+"/"+k, "finite numeric bound required")
			}
			out[k] = val
		case "minLength", "maxLength", "minItems", "maxItems", "minProperties", "maxProperties":
			n, ok := number(val)
			if !ok || !n.IsInt() || n.Sign() < 0 || n.Cmp(big.NewRat(10000000, 1)) > 0 {
				return nil, problem(path+"/"+k, "nonnegative bounded integer required")
			}
			out[k] = val
		case "pattern":
			s, ok := val.(string)
			if !ok || len(s) > 512 {
				return nil, problem(path+"/pattern", "bounded RE2 pattern required")
			}
			if _, e := regexp.Compile(s); e != nil {
				return nil, problem(path+"/pattern", "pattern unsupported by RE2")
			}
			out[k] = val
		case "format":
			s := text(val)
			if !strings.Contains(" uuid date date-time int32 int64 float double ", " "+s+" ") || s == "" {
				return nil, problem(path+"/format", "unsupported format")
			}
			out[k] = val
		case "uniqueItems":
			if _, ok := val.(bool); !ok {
				return nil, problem(path+"/uniqueItems", "boolean required")
			}
			out[k] = val
		case "nullable":
			if !c.v30 {
				return nil, problem(path+"/nullable", "use type with null in 3.1")
			}
			if _, ok := val.(bool); !ok {
				return nil, problem(path+"/nullable", "boolean required")
			}
		case "title", "description":
			if _, ok := val.(string); !ok {
				return nil, problem(path+"/"+k, "string required")
			}
			out[k] = val
		// Annotations are retained in the original document, never injected as input.
		case "default", "example", "examples", "deprecated", "readOnly", "writeOnly":
		}
	}
	if c.v30 {
		if truth(m["nullable"]) {
			typ := text(m["type"])
			if typ == "" {
				return nil, problem(path, "nullable needs an explicit type")
			}
			out["type"] = []any{typ, "null"}
		}
		for _, side := range []string{"Minimum", "Maximum"} {
			key := "exclusive" + side
			if truth(m[key]) {
				normal := strings.ToLower(side)
				n, ok := m[normal]
				if !ok {
					return nil, problem(path, "exclusive bound requires its numeric bound")
				}
				out[key] = n
				delete(out, normal)
			}
		}
	}
	if schemaType(out) == "array" {
		if _, ok := out["items"]; !ok {
			return nil, problem(path, "array items schema required")
		}
	}
	if truth(out["uniqueItems"]) {
		n, ok := number(out["maxItems"])
		if !ok || n.Cmp(big.NewRat(1000, 1)) > 0 {
			return nil, problem(path, "uniqueItems requires maxItems <= 1000")
		}
	}
	return out, nil
}
func number(v any) (*big.Rat, bool) {
	n, ok := v.(json.Number)
	if !ok {
		return nil, false
	}
	s := n.String()
	if len(s) > 128 {
		return nil, false
	}
	if _, e := n.Float64(); e != nil {
		return nil, false
	}
	r, ok := new(big.Rat).SetString(s)
	return r, ok
}
func schemaType(v any) string {
	m, _ := object(v)
	if s, ok := m["type"].(string); ok {
		return s
	}
	a, _ := m["type"].([]any)
	for _, v := range a {
		if s := text(v); s != "null" {
			return s
		}
	}
	return ""
}
func nullable(v any) bool {
	m, _ := object(v)
	a, _ := m["type"].([]any)
	for _, v := range a {
		if v == "null" {
			return true
		}
	}
	return m["type"] == "null"
}
func equal(a, b any) bool {
	if x, ok := number(a); ok {
		y, ok := number(b)
		return ok && x.Cmp(y) == 0
	}
	switch x := a.(type) {
	case map[string]any:
		y, ok := object(b)
		if !ok || len(x) != len(y) {
			return false
		}
		for k, v := range x {
			w, ok := y[k]
			if !ok || !equal(v, w) {
				return false
			}
		}
		return true
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i, v := range x {
			if !equal(v, y[i]) {
				return false
			}
		}
		return true
	}
	return reflect.DeepEqual(a, b)
}

// Validate checks only schemas produced by the compiler. No remote resolution,
// coercion, defaults, or silent removal of unknown input is performed.
func Validate(schema, value any) error { return validate(schema, value, 0) }
func validate(schema, value any, depth int) error {
	bad := func() error { return fmt.Errorf("schema mismatch") }
	if depth > 32 {
		return bad()
	}
	if b, ok := schema.(bool); ok {
		if b {
			return nil
		}
		return bad()
	}
	m, ok := object(schema)
	if !ok {
		return bad()
	}
	if v, has := m["const"]; has && !equal(v, value) {
		return bad()
	}
	if a, has := m["enum"].([]any); has {
		found := false
		for _, v := range a {
			if equal(v, value) {
				found = true
				break
			}
		}
		if !found {
			return bad()
		}
	}
	typ := schemaType(m)
	if value == nil {
		if typ == "" || nullable(m) {
			return nil
		}
		return bad()
	}
	switch typ {
	case "null":
		return bad()
	case "object":
		if _, ok := object(value); !ok {
			return bad()
		}
	case "array":
		if _, ok := value.([]any); !ok {
			return bad()
		}
	case "string":
		if _, ok := value.(string); !ok {
			return bad()
		}
	case "boolean":
		if _, ok := value.(bool); !ok {
			return bad()
		}
	case "integer", "number":
		n, ok := number(value)
		if !ok || typ == "integer" && !n.IsInt() {
			return bad()
		}
	}
	bounds := func(n int, low, high string) bool {
		v := big.NewRat(int64(n), 1)
		if a, ok := number(m[low]); ok && v.Cmp(a) < 0 {
			return false
		}
		if a, ok := number(m[high]); ok && v.Cmp(a) > 0 {
			return false
		}
		return true
	}
	switch v := value.(type) {
	case map[string]any:
		if !bounds(len(v), "minProperties", "maxProperties") {
			return bad()
		}
		req, _ := m["required"].([]any)
		for _, k := range req {
			if _, ok := v[text(k)]; !ok {
				return bad()
			}
		}
		props, _ := object(m["properties"])
		for k, val := range v {
			p, ok := props[k]
			if !ok {
				p, ok = m["additionalProperties"]
				if !ok {
					continue
				}
			}
			if validate(p, val, depth+1) != nil {
				return bad()
			}
		}
	case []any:
		if !bounds(len(v), "minItems", "maxItems") {
			return bad()
		}
		p, has := m["items"]
		for i, val := range v {
			if has && validate(p, val, depth+1) != nil {
				return bad()
			}
			if truth(m["uniqueItems"]) {
				for _, other := range v[:i] {
					if equal(other, val) {
						return bad()
					}
				}
			}
		}
	case string:
		if !bounds(utf8.RuneCountInString(v), "minLength", "maxLength") {
			return bad()
		}
		if pat, ok := m["pattern"].(string); ok {
			r, e := regexp.Compile(pat)
			if e != nil || !r.MatchString(v) {
				return bad()
			}
		}
		switch text(m["format"]) {
		case "uuid":
			if !regexp.MustCompile(`(?i)^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`).MatchString(v) {
				return bad()
			}
		case "date":
			if _, e := time.Parse("2006-01-02", v); e != nil {
				return bad()
			}
		case "date-time":
			if _, e := time.Parse(time.RFC3339Nano, v); e != nil {
				return bad()
			}
		}
	case json.Number:
		n, ok := number(v)
		if !ok {
			return bad()
		}
		for _, key := range []string{"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"} {
			b, ok := number(m[key])
			if !ok {
				continue
			}
			d := n.Cmp(b)
			if key == "minimum" && d < 0 || key == "maximum" && d > 0 || key == "exclusiveMinimum" && d <= 0 || key == "exclusiveMaximum" && d >= 0 {
				return bad()
			}
		}
		if b, ok := number(m["multipleOf"]); ok && !new(big.Rat).Quo(n, b).IsInt() {
			return bad()
		}
		if f := text(m["format"]); f == "int32" || f == "int64" {
			bits := uint(63)
			if f == "int32" {
				bits = 31
			}
			upper := new(big.Int).Lsh(big.NewInt(1), bits)
			if !n.IsInt() || n.Num().Cmp(upper) >= 0 || n.Num().Cmp(new(big.Int).Neg(upper)) < 0 {
				return bad()
			}
		}
	}
	return nil
}
