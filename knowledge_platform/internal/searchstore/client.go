// Package searchstore owns only Knowledge physical indexes. Model inference is external.
package searchstore

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"github.com/google/uuid"
	"io"
	"knowledge-platform/internal/indexing"
	"net"
	"net/http"
	"net/url"
	"reflect"
	"strings"
	"time"
)

type Client struct {
	Base, User, Password string
	HTTP                 *http.Client
}
type HTTPError struct {
	Code      int
	Type      string
	Parameter string
}

func (e *HTTPError) Error() string {
	message := fmt.Sprintf("OPENSEARCH_HTTP_%d", e.Code)
	if e.Type != "" {
		message += ": " + e.Type
	}
	if e.Parameter != "" {
		message += "; unknown parameter [" + e.Parameter + "]"
	}
	return message
}

// Expose bounded diagnostic identifiers, never arbitrary reasons containing
// document text, vectors, credentials or the full upstream response.
func diagnosticToken(s string) bool {
	if len(s) == 0 || len(s) > 80 {
		return false
	}
	for _, c := range s {
		if !(c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' || c == '_' || c == '.' || c == '-') {
			return false
		}
	}
	return true
}
func responseError(code int, raw []byte) *HTTPError {
	out := &HTTPError{Code: code}
	type cause struct {
		Type   string `json:"type"`
		Reason string `json:"reason"`
	}
	var body struct {
		Error struct {
			Type      string  `json:"type"`
			Reason    string  `json:"reason"`
			RootCause []cause `json:"root_cause"`
		} `json:"error"`
	}
	if json.Unmarshal(raw, &body) != nil {
		return out
	}
	candidates := append([]cause{{body.Error.Type, body.Error.Reason}}, body.Error.RootCause...)
	for _, item := range candidates {
		if out.Type == "" && diagnosticToken(item.Type) {
			out.Type = item.Type
		}
		const marker = "contains an unknown parameter ["
		if at := strings.Index(item.Reason, marker); out.Parameter == "" && at >= 0 {
			tail := item.Reason[at+len(marker):]
			if end := strings.IndexByte(tail, ']'); end >= 0 && diagnosticToken(tail[:end]) {
				out.Parameter = tail[:end]
			}
		}
	}
	return out
}
func New(endpoint, user, password, caBase64 string) (*Client, error) {
	u, e := url.Parse(endpoint)
	if e != nil || u.Scheme != "https" || u.Host == "" || u.User != nil || u.Path != "" && u.Path != "/" || u.RawQuery != "" || u.Fragment != "" || user == "" || password == "" {
		return nil, fmt.Errorf("OpenSearch HTTPS endpoint and credentials required")
	}
	ca, e := base64.StdEncoding.DecodeString(caBase64)
	if e != nil {
		return nil, fmt.Errorf("OpenSearch CA is invalid")
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("OpenSearch CA is invalid")
	}
	transport := &http.Transport{Proxy: nil, TLSClientConfig: &tls.Config{MinVersion: tls.VersionTLS12, RootCAs: roots}, DialContext: (&net.Dialer{Timeout: 5 * time.Second}).DialContext, TLSHandshakeTimeout: 5 * time.Second, ResponseHeaderTimeout: 35 * time.Second}
	return &Client{strings.TrimRight(endpoint, "/"), user, password, &http.Client{Transport: transport, Timeout: 45 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("OpenSearch redirect refused") }}}, nil
}
func names(id string) (string, string, error) {
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id {
		return "", "", fmt.Errorf("invalid index version")
	}
	suffix := strings.ReplaceAll(id, "-", "")
	return "knowledge-v1-" + suffix, "knowledge-write-" + suffix, nil
}
func (c *Client) request(ctx context.Context, method, path string, body []byte, out any) error {
	req, e := http.NewRequestWithContext(ctx, method, c.Base+path, bytes.NewReader(body))
	if e != nil {
		return fmt.Errorf("OpenSearch request invalid")
	}
	req.SetBasicAuth(c.User, c.Password)
	req.Header.Set("Content-Type", "application/json")
	if strings.Contains(path, "_bulk") {
		req.Header.Set("Content-Type", "application/x-ndjson")
	}
	res, e := c.HTTP.Do(req)
	if e != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return fmt.Errorf("OPENSEARCH_UNAVAILABLE: request result unconfirmed")
	}
	defer res.Body.Close()
	raw, e := io.ReadAll(io.LimitReader(res.Body, (2<<20)+1))
	if e != nil {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		return ErrInvalidResponse
	}
	if len(raw) > 2<<20 {
		return ErrResponseTooLarge
	}
	if res.StatusCode < 200 || res.StatusCode >= 300 {
		return responseError(res.StatusCode, raw)
	}
	if out != nil && json.Unmarshal(raw, out) != nil {
		return ErrInvalidResponse
	}
	return nil
}
func Mapping(v indexing.Version) map[string]any {
	config, _ := v.EffectiveConfig() // inspect validates before any OpenSearch request.
	props := map[string]any{}
	for _, name := range []string{"chunk_id", "index_version_id", "embedding_space_id", "source_document_id", "canonical_id", "kind", "content_sha256"} {
		props[name] = map[string]any{"type": "keyword"}
	}
	props["ordinal"] = map[string]any{"type": "integer"}
	props["input_tokens"] = map[string]any{"type": "integer"}
	props["text"] = map[string]any{"type": "text", "index": false}
	props["provenance"] = map[string]any{"type": "object", "enabled": false}
	props["vector"] = map[string]any{"type": "knn_vector", "dimension": v.Profile.Dimension, "method": map[string]any{"name": config.Method, "engine": config.Engine, "space_type": config.SpaceType, "parameters": map[string]any{"ef_construction": config.Parameters.EFConstruction, "m": config.Parameters.M}}}
	return map[string]any{"dynamic": "strict", "_meta": map[string]any{"owner": "knowledge-platform", "schema": "index-mapping-v1", "index_version_id": v.ID, "embedding_space_id": v.Profile.EmbeddingSpaceId}, "properties": props}
}
func normalized(v any) any { b, _ := json.Marshal(v); var x any; _ = json.Unmarshal(b, &x); return x }

// OpenSearch may render the implicit float vector data type explicitly.
// Normalize only that documented default; all other mapping fields stay exact.
func sameMapping(actual, expected map[string]any) bool {
	clean := func(value map[string]any) any {
		if value == nil {
			return nil
		}
		x := normalized(value).(map[string]any)
		if props, ok := x["properties"].(map[string]any); ok {
			if vector, ok := props["vector"].(map[string]any); ok && vector["data_type"] == "float" {
				delete(vector, "data_type")
			}
		}
		return x
	}
	return reflect.DeepEqual(clean(actual), clean(expected))
}
func (c *Client) inspect(ctx context.Context, v indexing.Version) (bool, error) {
	if _, e := v.EffectiveConfig(); e != nil {
		return false, e
	}
	name, alias, e := names(v.ID)
	if e != nil {
		return false, e
	}
	var result map[string]struct {
		Mappings map[string]any             `json:"mappings"`
		Aliases  map[string]json.RawMessage `json:"aliases"`
		Settings struct {
			Index struct {
				KNN string `json:"knn"`
			} `json:"index"`
		} `json:"settings"`
	}
	if e = c.request(ctx, "GET", "/"+name, nil, &result); e != nil {
		if h, ok := e.(*HTTPError); ok && h.Code == 404 {
			return false, nil
		}
		return false, e
	}
	got, ok := result[name]
	if !ok || len(result) != 1 || !sameMapping(got.Mappings, Mapping(v)) || got.Settings.Index.KNN != "true" {
		return false, fmt.Errorf("OPENSEARCH_OWNERSHIP_CONFLICT: physical mapping differs")
	}
	if _, ok = got.Aliases[alias]; !ok || len(got.Aliases) != 1 {
		return false, fmt.Errorf("OPENSEARCH_OWNERSHIP_CONFLICT: write alias differs")
	}
	return true, nil
}
func (c *Client) Ensure(ctx context.Context, v indexing.Version) error {
	exists, e := c.inspect(ctx, v)
	if e != nil {
		return e
	}
	if exists {
		return nil
	}
	var info struct {
		Version struct {
			Number       string `json:"number"`
			Distribution string `json:"distribution"`
		} `json:"version"`
	}
	if e = c.request(ctx, "GET", "/", nil, &info); e != nil {
		return e
	}
	if info.Version.Distribution != "opensearch" || !strings.HasPrefix(info.Version.Number, "3.") {
		return fmt.Errorf("OPENSEARCH_VERSION_UNSUPPORTED: expected OpenSearch 3.x")
	}
	name, alias, _ := names(v.ID)
	body, _ := json.Marshal(map[string]any{"settings": map[string]any{"index.knn": true, "number_of_shards": 1, "number_of_replicas": 1}, "mappings": Mapping(v), "aliases": map[string]any{alias: map[string]any{"is_write_index": true}}})
	var ack struct {
		Acknowledged bool `json:"acknowledged"`
	}
	if e = c.request(ctx, "PUT", "/"+name, body, &ack); e != nil {
		return e
	}
	if !ack.Acknowledged {
		return fmt.Errorf("OPENSEARCH_CREATE_UNCONFIRMED")
	}
	exists, e = c.inspect(ctx, v)
	if e != nil {
		return e
	}
	if !exists {
		return fmt.Errorf("OPENSEARCH_CREATE_UNCONFIRMED")
	}
	return nil
}
func (c *Client) Bulk(ctx context.Context, v indexing.Version, records []indexing.Record) error {
	name, alias, e := names(v.ID)
	if e != nil {
		return e
	}
	var body bytes.Buffer
	for _, r := range records {
		a, _ := json.Marshal(map[string]any{"index": map[string]any{"_index": alias, "_id": r.ID, "require_alias": true}})
		b, e := json.Marshal(r)
		if e != nil {
			return fmt.Errorf("invalid index document")
		}
		body.Write(a)
		body.WriteByte('\n')
		body.Write(b)
		body.WriteByte('\n')
	}
	if body.Len() > 8<<20 {
		return fmt.Errorf("OPENSEARCH_BATCH_TOO_LARGE")
	}
	var response struct {
		Errors *bool `json:"errors"`
		Items  []map[string]struct {
			ID     string          `json:"_id"`
			Index  string          `json:"_index"`
			Status int             `json:"status"`
			Error  json.RawMessage `json:"error"`
			Shards struct {
				Total      int `json:"total"`
				Successful int `json:"successful"`
				Failed     int `json:"failed"`
			} `json:"_shards"`
		} `json:"items"`
	}
	if e = c.request(ctx, "POST", "/"+alias+"/_bulk?require_alias=true&wait_for_active_shards=all&timeout=30s", body.Bytes(), &response); e != nil {
		return e
	}
	if response.Errors == nil || *response.Errors || len(response.Items) != len(records) {
		return fmt.Errorf("OPENSEARCH_BULK_INCOMPLETE: not all items succeeded")
	}
	for i, item := range response.Items {
		row, ok := item["index"]
		if !ok || len(item) != 1 || row.ID != records[i].ID || row.Index != name || row.Status != 200 && row.Status != 201 || len(row.Error) > 0 && string(row.Error) != "null" || row.Shards.Failed != 0 || row.Shards.Successful < 1 || row.Shards.Total < row.Shards.Successful {
			return fmt.Errorf("OPENSEARCH_BULK_INCOMPLETE: item identity or shard failure")
		}
	}
	return nil
}

type shards struct {
	Total      int `json:"total"`
	Successful int `json:"successful"`
	Failed     int `json:"failed"`
}

func (c *Client) Seal(ctx context.Context, v indexing.Version) error {
	exists, e := c.inspect(ctx, v)
	if e != nil {
		return e
	}
	if !exists {
		return fmt.Errorf("OPENSEARCH_INDEX_MISSING")
	}
	name, _, _ := names(v.ID)
	var blocked struct {
		Acknowledged       bool `json:"acknowledged"`
		ShardsAcknowledged bool `json:"shards_acknowledged"`
		Indices            []struct {
			Name    string `json:"name"`
			Blocked bool   `json:"blocked"`
		} `json:"indices"`
	}
	if e = c.request(ctx, "PUT", "/"+name+"/_block/write", nil, &blocked); e != nil {
		return e
	}
	if !blocked.Acknowledged || !blocked.ShardsAcknowledged || len(blocked.Indices) != 1 || blocked.Indices[0].Name != name || !blocked.Indices[0].Blocked {
		return fmt.Errorf("OPENSEARCH_SEAL_UNCONFIRMED")
	}
	var refresh struct {
		Shards shards `json:"_shards"`
	}
	if e = c.request(ctx, "POST", "/"+name+"/_refresh", nil, &refresh); e != nil {
		return e
	}
	if refresh.Shards.Failed != 0 || refresh.Shards.Successful < 1 || refresh.Shards.Total != refresh.Shards.Successful {
		return fmt.Errorf("OPENSEARCH_REFRESH_INCOMPLETE")
	}
	var count struct {
		Count  *int   `json:"count"`
		Shards shards `json:"_shards"`
	}
	if e = c.request(ctx, "GET", "/"+name+"/_count", nil, &count); e != nil {
		return e
	}
	if count.Count == nil || *count.Count != v.Count || count.Shards.Failed != 0 || count.Shards.Successful < 1 || count.Shards.Total != count.Shards.Successful {
		return fmt.Errorf("OPENSEARCH_COUNT_MISMATCH")
	}
	return nil
}
func (c *Client) Delete(ctx context.Context, v indexing.Version) error {
	exists, e := c.inspect(ctx, v)
	if e != nil {
		return e
	}
	if !exists {
		return nil
	}
	name, _, _ := names(v.ID)
	var ack struct {
		Acknowledged bool `json:"acknowledged"`
	}
	if e = c.request(ctx, "DELETE", "/"+name, nil, &ack); e != nil {
		if h, ok := e.(*HTTPError); ok && h.Code == 404 {
			return nil
		}
		return e
	}
	if !ack.Acknowledged {
		return fmt.Errorf("OPENSEARCH_DELETE_UNCONFIRMED")
	}
	exists, e = c.inspect(ctx, v)
	if e != nil {
		return e
	}
	if exists {
		return fmt.Errorf("OPENSEARCH_DELETE_UNCONFIRMED")
	}
	return nil
}
