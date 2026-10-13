package apispec

import (
	"encoding/json"
	"fmt"
	yaml "go.yaml.in/yaml/v3"
	"io"
	"net/url"
	"regexp"
	"sort"
	"strings"
	"unicode/utf8"
)

type Parameter struct {
	Name     string `json:"name"`
	In       string `json:"in"`
	Required bool   `json:"required"`
	Explode  bool   `json:"explode"`
	Schema   any    `json:"schema"`
}
type Auth struct {
	Name string `json:"name"`
	Type string `json:"type"`
	In   string `json:"in,omitempty"`
	Key  string `json:"key,omitempty"`
}
type Operation struct {
	Key          string         `json:"key"`
	Method       string         `json:"method"`
	Path         string         `json:"path"`
	OperationID  string         `json:"operation_id"`
	Summary      string         `json:"summary"`
	Supported    bool           `json:"supported"`
	Problems     []Problem      `json:"problems"`
	Servers      []string       `json:"servers"`
	Parameters   []Parameter    `json:"parameters"`
	AuthOptions  []Auth         `json:"auth_options"`
	InputSchema  any            `json:"input_schema"`
	Responses    map[string]any `json:"responses"`
	BodyRequired bool           `json:"body_required"`
}
type Analysis struct {
	SpecVersion string      `json:"spec_version"`
	Title       string      `json:"title"`
	Operations  []Operation `json:"operations"`
	Problems    []Problem   `json:"problems"`
}

func YAML(raw []byte) (any, error) {
	if len(raw) > MaxDocument || !utf8.Valid(raw) {
		return nil, problem("/", "invalid UTF-8 or size limit")
	}
	decoder := yaml.NewDecoder(strings.NewReader(string(raw)))
	var root yaml.Node
	if e := decoder.Decode(&root); e != nil {
		return nil, problem("/", "invalid YAML")
	}
	var extra yaml.Node
	if e := decoder.Decode(&extra); e != io.EOF {
		return nil, problem("/", "exactly one document required")
	}
	count := 0
	var convert func(*yaml.Node, int) (any, error)
	convert = func(n *yaml.Node, depth int) (any, error) {
		count++
		if count > 100000 || depth > 48 {
			return nil, problem("/", "document depth or node limit")
		}
		if n.Anchor != "" || n.Kind == yaml.AliasNode {
			return nil, problem("/", "YAML anchors and aliases are not supported")
		}
		switch n.Kind {
		case yaml.DocumentNode:
			if len(n.Content) != 1 {
				return nil, problem("/", "one document required")
			}
			return convert(n.Content[0], depth+1)
		case yaml.MappingNode:
			out := map[string]any{}
			for i := 0; i < len(n.Content); i += 2 {
				k := n.Content[i]
				if k.Kind != yaml.ScalarNode || (k.Tag != "!!str" && k.Tag != "!!int") {
					return nil, problem("/", "string keys required")
				}
				key := k.Value
				if _, ok := out[key]; ok {
					return nil, problem("/", "duplicate key")
				}
				v, e := convert(n.Content[i+1], depth+1)
				if e != nil {
					return nil, e
				}
				out[key] = v
			}
			return out, nil
		case yaml.SequenceNode:
			out := []any{}
			for _, item := range n.Content {
				v, e := convert(item, depth+1)
				if e != nil {
					return nil, e
				}
				out = append(out, v)
			}
			return out, nil
		case yaml.ScalarNode:
			switch n.Tag {
			case "!!str":
				return n.Value, nil
			case "!!null":
				return nil, nil
			case "!!bool":
				var b bool
				if n.Decode(&b) != nil {
					return nil, problem("/", "invalid boolean")
				}
				return b, nil
			case "!!int", "!!float":
				// Require JSON-compatible numbers; timestamps/binary/custom tags are refused.
				var value any
				if json.Unmarshal([]byte(n.Value), &value) != nil {
					return nil, problem("/", "use JSON-compatible numbers")
				}
				v, e := JSON([]byte(n.Value), 256)
				if e != nil {
					return nil, e
				}
				if _, ok := number(v); !ok {
					return nil, problem("/", "finite number required")
				}
				return v, nil
			}
		}
		return nil, problem("/", "unsupported YAML tag")
	}
	return convert(&root, 0)
}
func Parse(raw []byte, format string) (*Analysis, error) {
	var v any
	var e error
	switch format {
	case "json":
		v, e = JSON(raw, MaxDocument)
	case "yaml":
		v, e = YAML(raw)
	default:
		return nil, problem("/", "format must be json or yaml")
	}
	if e != nil {
		return nil, e
	}
	root, ok := object(v)
	if !ok {
		return nil, problem("/", "object required")
	}
	ver := text(root["openapi"])
	if !regexp.MustCompile(`^3\.[01]\.[0-9]+$`).MatchString(ver) {
		return nil, problem("/openapi", "only OpenAPI 3.0.x and 3.1.x are supported")
	}
	info, ok := object(root["info"])
	if !ok || text(info["title"]) == "" || text(info["version"]) == "" {
		return nil, problem("/info", "title and version required")
	}
	paths, ok := object(root["paths"])
	if !ok {
		return nil, problem("/paths", "paths object required")
	}
	a := &Analysis{SpecVersion: ver, Title: text(info["title"]), Operations: []Operation{}, Problems: []Problem{}}
	rootErr := keys(root, "openapi info paths components servers security tags externalDocs jsonSchemaDialect", "/")
	if dialect, has := root["jsonSchemaDialect"]; has && dialect != "https://spec.openapis.org/oas/3.1/dialect/base" {
		rootErr = problem("/jsonSchemaDialect", "custom dialect unsupported")
	}
	c := &compiler{root: root, v30: strings.HasPrefix(ver, "3.0.")}
	names := []string{}
	for path := range paths {
		if strings.HasPrefix(path, "x-") {
			continue
		}
		names = append(names, path)
	}
	sort.Strings(names)
	for _, path := range names {
		item, refs, e := c.deref(paths[path], "/paths/"+pointer(path), nil)
		_ = refs
		pi, ok := object(item)
		if e != nil || !ok {
			p := Problem{"/paths/" + pointer(path), "unsupported path item"}
			if x, ok := e.(*Problem); ok {
				p = *x
			}
			a.Problems = append(a.Problems, p)
			continue
		}
		for _, method := range []string{"get", "post", "put", "patch", "delete", "head", "options", "trace"} {
			rawOp, exists := pi[method]
			if !exists {
				continue
			}
			if len(a.Operations) >= 200 {
				return nil, problem("/paths", "maximum 200 operations")
			}
			op := Operation{Key: strings.ToUpper(method) + " " + path, Method: strings.ToUpper(method), Path: path, Problems: []Problem{}, Parameters: []Parameter{}, Servers: []string{}, AuthOptions: []Auth{}}
			loc := "/paths/" + pointer(path) + "/" + method
			c.nodes = 0
			err := rootErr
			if err == nil {
				err = c.operation(&op, pi, rawOp, loc)
			}
			if err != nil {
				p := Problem{loc, "unsupported operation"}
				if x, ok := err.(*Problem); ok {
					p = *x
				}
				op.Problems = append(op.Problems, p)
			} else {
				op.Supported = true
			}
			encoded, _ := json.Marshal(op)
			if len(encoded) > MaxOperation {
				return nil, problem(loc, "expanded operation exceeds 64 KiB")
			}
			a.Operations = append(a.Operations, op)
		}
	}
	if len(a.Operations) == 0 && len(a.Problems) == 0 {
		return nil, problem("/paths", "no operations found")
	}
	b, _ := json.Marshal(a)
	if len(b) > MaxAnalysis {
		return nil, problem("/paths", "analysis exceeds 1 MiB")
	}
	return a, nil
}
func blockedHeader(name string) bool {
	n := strings.ToLower(name)
	return strings.HasPrefix(n, "proxy-") || strings.HasPrefix(n, "sec-") || strings.HasPrefix(n, "x-forwarded-") || strings.Contains(" host authorization cookie set-cookie content-type content-length transfer-encoding connection upgrade te trailer accept accept-encoding expect forwarded idempotency-key ", " "+n+" ")
}
func (c *compiler) operation(out *Operation, pi map[string]any, raw any, path string) error {
	op, ok := object(raw)
	if !ok {
		return problem(path, "operation object required")
	}
	out.OperationID = text(op["operationId"])
	out.Summary = text(op["summary"])
	if out.Method == "HEAD" || out.Method == "OPTIONS" || out.Method == "TRACE" {
		return problem(path, "method is outside MVP")
	}
	if e := keys(pi, "get post put patch delete head options trace parameters servers summary description", path); e != nil {
		return e
	}
	if e := keys(op, "tags summary description externalDocs operationId parameters requestBody responses deprecated security servers", path); e != nil {
		return e
	}
	if !strings.HasPrefix(out.Path, "/") || strings.ContainsAny(out.Path, "?#\\\r\n\x00") || strings.Contains(out.Path, "%") || len(out.Path) > 2048 {
		return problem(path, "unsafe path template")
	}
	for _, part := range strings.Split(out.Path, "/") {
		if part == "." || part == ".." {
			return problem(path, "dot path segments unsupported")
		}
	}
	props := map[string]any{}
	required := []any{}
	byLocation := map[string]map[string]any{"path": {}, "query": {}, "header": {}}
	requiredLocation := map[string][]any{"path": {}, "query": {}, "header": {}}
	all := map[string]Parameter{}
	for _, owner := range []map[string]any{pi, op} {
		if values, exists := owner["parameters"]; exists {
			list, ok := values.([]any)
			if !ok || len(list) > 100 {
				return problem(path+"/parameters", "maximum 100 parameters")
			}
			seen := map[string]bool{}
			for i, item := range list {
				loc := fmt.Sprintf("%s/parameters/%d", path, i)
				v, refs, e := c.deref(item, loc, nil)
				if e != nil {
					return e
				}
				p, ok := object(v)
				if !ok {
					return problem(loc, "parameter object required")
				}
				if e = keys(p, "name in description required deprecated allowEmptyValue style explode allowReserved schema example examples", loc); e != nil {
					return e
				}
				name, where := text(p["name"]), text(p["in"])
				if name == "" || len(name) > 128 || strings.ContainsAny(name, "\r\n\x00") {
					return problem(loc, "invalid parameter name")
				}
				if where != "path" && where != "query" && where != "header" {
					return problem(loc, "only path/query/header parameters supported")
				}
				if where == "header" && (blockedHeader(name) || !regexp.MustCompile(`^[A-Za-z0-9!#$&'*+.^_|~-]+$`).MatchString(name)) {
					return problem(loc, "reserved or invalid header")
				}
				if truth(p["allowReserved"]) || truth(p["allowEmptyValue"]) {
					return problem(loc, "allowReserved/allowEmptyValue unsupported")
				}
				for _, k := range []string{"required", "explode", "allowReserved", "allowEmptyValue"} {
					if val, has := p[k]; has {
						if _, ok := val.(bool); !ok {
							return problem(loc+"/"+k, "boolean required")
						}
					}
				}
				schema, e := c.schema(p["schema"], loc+"/schema", refs, 0)
				if e != nil {
					return e
				}
				typ := schemaType(schema)
				if nullable(schema) || typ == "object" || typ == "" || typ == "null" {
					return problem(loc, "parameter must have a non-null scalar or query array schema")
				}
				if val, has := p["style"]; has {
					if _, ok := val.(string); !ok {
						return problem(loc+"/style", "string required")
					}
				}
				style := text(p["style"])
				if style == "" {
					if where == "query" {
						style = "form"
					} else {
						style = "simple"
					}
				}
				explode := style == "form"
				if b, has := p["explode"].(bool); has {
					explode = b
				}
				if (where == "query" && style != "form") || (where != "query" && style != "simple") {
					return problem(loc, "unsupported serialization style")
				}
				if typ == "array" {
					m, _ := object(schema)
					itemType := schemaType(m["items"])
					if where != "query" || !explode || itemType == "object" || itemType == "array" || itemType == "" || nullable(m["items"]) {
						return problem(loc, "only form/explode query arrays of non-null scalars supported")
					}
				}
				req := truth(p["required"])
				if where == "path" && !req {
					return problem(loc, "path parameters must be required")
				}
				key := where + ":" + name
				if where == "header" {
					key = strings.ToLower(key)
				}
				if seen[key] {
					return problem(loc, "duplicate parameter")
				}
				seen[key] = true
				all[key] = Parameter{Name: name, In: where, Required: req, Explode: explode, Schema: schema}
			}
		}
	}
	ordered := []string{}
	for k := range all {
		ordered = append(ordered, k)
	}
	sort.Strings(ordered)
	for _, k := range ordered {
		p := all[k]
		byLocation[p.In][p.Name] = p.Schema
		if p.Required {
			requiredLocation[p.In] = append(requiredLocation[p.In], p.Name)
		}
		out.Parameters = append(out.Parameters, p)
	}
	for _, where := range []string{"path", "query", "header"} {
		if len(byLocation[where]) > 0 {
			props[where] = map[string]any{"type": "object", "properties": byLocation[where], "required": requiredLocation[where], "additionalProperties": false}
			if len(requiredLocation[where]) > 0 {
				required = append(required, where)
			}
		}
	}
	template := regexp.MustCompile(`\{([^{}]+)\}`)
	matches := template.FindAllStringSubmatch(out.Path, -1)
	used := map[string]bool{}
	for _, m := range matches {
		if _, ok := all["path:"+m[1]]; !ok {
			return problem(path, "path placeholder has no parameter")
		}
		used[m[1]] = true
	}
	if strings.ContainsAny(template.ReplaceAllString(out.Path, ""), "{}") {
		return problem(path, "invalid path braces")
	}
	for _, p := range out.Parameters {
		if p.In == "path" && !used[p.Name] {
			return problem(path, "unused path parameter")
		}
	}
	if body, has := op["requestBody"]; has {
		if out.Method == "GET" {
			return problem(path+"/requestBody", "GET request body unsupported")
		}
		v, refs, e := c.deref(body, path+"/requestBody", nil)
		if e != nil {
			return e
		}
		m, ok := object(v)
		if !ok {
			return problem(path, "request body object required")
		}
		if e = keys(m, "description required content", path+"/requestBody"); e != nil {
			return e
		}
		if val, has := m["required"]; has {
			if _, ok := val.(bool); !ok {
				return problem(path, "body required must be boolean")
			}
		}
		schema, e := c.content(m["content"], path+"/requestBody/content", refs)
		if e != nil {
			return e
		}
		props["body"] = schema
		out.BodyRequired = truth(m["required"])
		if out.BodyRequired {
			required = append(required, "body")
		}
	}
	out.InputSchema = map[string]any{"type": "object", "properties": props, "required": required, "additionalProperties": false}
	responses, ok := object(op["responses"])
	if !ok {
		return problem(path+"/responses", "responses object required")
	}
	out.Responses = map[string]any{}
	codes := []string{}
	for code := range responses {
		codes = append(codes, code)
	}
	sort.Strings(codes)
	for _, code := range codes {
		if strings.HasPrefix(code, "x-") {
			continue
		}
		if !regexp.MustCompile(`^[1-5][0-9]{2}$`).MatchString(code) && code != "default" {
			return problem(path+"/responses/"+code, "explicit status codes required")
		}
		if !strings.HasPrefix(code, "2") {
			continue
		}
		if code == "202" {
			return problem(path+"/responses/202", "asynchronous operation unsupported")
		}
		v, refs, e := c.deref(responses[code], path+"/responses/"+code, nil)
		if e != nil {
			return e
		}
		m, ok := object(v)
		if !ok {
			return problem(path, "response object required")
		}
		if e = keys(m, "description content headers links", path+"/responses/"+code); e != nil {
			return e
		}
		if _, ok := m["description"].(string); !ok {
			return problem(path, "response description required")
		}
		if code == "204" {
			if content, has := m["content"]; has {
				obj, ok := object(content)
				if !ok || len(obj) > 0 {
					return problem(path, "204 cannot have a response body")
				}
			}
			out.Responses[code] = nil
			continue
		}
		schema, e := c.content(m["content"], path+"/responses/"+code+"/content", refs)
		if e != nil {
			return e
		}
		out.Responses[code] = schema
	}
	if len(out.Responses) == 0 {
		return problem(path+"/responses", "a synchronous 2xx JSON or 204 response is required")
	}
	security, has := op["security"]
	if !has {
		security, has = c.root["security"]
	}
	if has && security == nil {
		return problem(path+"/security", "security must be an array")
	}
	auth, e := c.auth(security, path+"/security")
	if e != nil {
		return e
	}
	out.AuthOptions = auth
	servers, has := op["servers"]
	if !has {
		servers, has = pi["servers"]
	}
	if !has {
		servers = c.root["servers"]
	}
	if servers != nil {
		list, ok := servers.([]any)
		if !ok || len(list) > 20 {
			return problem(path+"/servers", "maximum 20 server candidates")
		}
		for _, v := range list {
			m, ok := object(v)
			if !ok {
				return problem(path, "invalid server")
			}
			url := text(m["url"])
			if url == "" {
				return problem(path, "server URL required")
			}
			vars, _ := object(m["variables"])
			for name, v := range vars {
				variable, ok := object(v)
				if !ok {
					return problem(path, "invalid server variable")
				}
				def, ok := variable["default"].(string)
				if !ok {
					return problem(path, "server variable default required")
				}
				if vals, has := variable["enum"]; has {
					a, ok := vals.([]any)
					if !ok {
						return problem(path, "invalid server variable enum")
					}
					found := false
					for _, val := range a {
						if val == def {
							found = true
						}
					}
					if !found {
						return problem(path, "server default not in enum")
					}
				}
				url = strings.ReplaceAll(url, "{"+name+"}", def)
			}
			if strings.ContainsAny(url, "{}\r\n\x00") {
				return problem(path, "unresolved server variable")
			}
			parsed, e := urlpkgParse(url)
			if e != nil {
				return problem(path+"/servers", "server URL must not include credentials, query or fragment")
			}
			_ = parsed
			out.Servers = append(out.Servers, url)
		}
	}
	return nil
}
func (c *compiler) content(v any, path string, seen map[string]bool) (any, error) {
	m, ok := object(v)
	if !ok || len(m) != 1 {
		return nil, problem(path, "only application/json content is supported")
	}
	media, ok := object(m["application/json"])
	if !ok {
		return nil, problem(path, "application/json required")
	}
	if e := keys(media, "schema example examples", path+"/application~1json"); e != nil {
		return nil, e
	}
	return c.schema(media["schema"], path+"/application~1json/schema", seen, 0)
}
func (c *compiler) auth(v any, path string) ([]Auth, error) {
	none := []Auth{{Name: "none", Type: "none"}}
	if v == nil {
		return none, nil
	}
	list, ok := v.([]any)
	if !ok || len(list) > 20 {
		return nil, problem(path, "invalid security alternatives")
	}
	if len(list) == 0 {
		return none, nil
	}
	comp, _ := object(c.root["components"])
	schemes, _ := object(comp["securitySchemes"])
	out := []Auth{}
	for _, item := range list {
		m, ok := object(item)
		if !ok || len(m) > 1 {
			return nil, problem(path, "multiple simultaneous security schemes unsupported")
		}
		if len(m) == 0 {
			out = append(out, none[0])
			continue
		}
		for name, scopes := range m {
			a, ok := scopes.([]any)
			if !ok || len(a) > 0 {
				return nil, problem(path, "OAuth scopes unsupported")
			}
			v, _, e := c.deref(schemes[name], path, nil)
			if e != nil {
				return nil, e
			}
			s, ok := object(v)
			if !ok {
				return nil, problem(path, "security scheme not found")
			}
			switch text(s["type"]) {
			case "http":
				if strings.ToLower(text(s["scheme"])) != "bearer" {
					return nil, problem(path, "only HTTP Bearer supported")
				}
				out = append(out, Auth{Name: name, Type: "bearer"})
			case "apiKey":
				where, key := text(s["in"]), text(s["name"])
				if !regexp.MustCompile(`^[A-Za-z][A-Za-z0-9_.-]{0,63}$`).MatchString(key) {
					return nil, problem(path, "API key name outside supported token format")
				}
				if (where != "header" && where != "query") || key == "" || where == "header" && blockedHeader(key) {
					return nil, problem(path, "unsupported API key location/name")
				}
				out = append(out, Auth{Name: name, Type: "api_key", In: where, Key: key})
			default:
				return nil, problem(path, "only none, Bearer and API key authentication supported")
			}
		}
	}
	return out, nil
}

func urlpkgParse(value string) (*url.URL, error) {
	u, e := url.Parse(value)
	if e != nil {
		return nil, e
	}
	if u.User != nil || u.RawQuery != "" || u.Fragment != "" || (u.Scheme != "" && u.Scheme != "http" && u.Scheme != "https") {
		return nil, fmt.Errorf("unsafe server URL")
	}
	return u, nil
}
