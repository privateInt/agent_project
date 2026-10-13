package app

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"knowledge-platform/internal/chunking"
	"knowledge-platform/internal/retrieval"
)

// ModelConsole transports bounded, read-only model selection and query embedding for the browser.
// It never reads Knowledge state, adds prefixes, or performs search.
type ModelConsole struct {
	base   string
	client *http.Client
}

func NewModelConsole(endpoint string) (*ModelConsole, error) {
	u, err := url.Parse(endpoint)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" || u.User != nil || (u.Path != "" && u.Path != "/") || u.RawQuery != "" || u.Fragment != "" || u.Opaque != "" {
		return nil, fmt.Errorf("Model console requires a fixed Model Gateway HTTP(S) origin")
	}
	return &ModelConsole{base: strings.TrimRight(endpoint, "/"), client: &http.Client{
		Timeout:       90 * time.Second,
		CheckRedirect: func(*http.Request, []*http.Request) error { return fmt.Errorf("Model redirect refused") },
	}}, nil
}

var queryInputID = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$`)

func validateConsoleQuery(raw []byte) bool {
	var fields map[string]json.RawMessage
	if chunking.Decode(raw, &fields) != nil || len(fields) != 3 || fields["expected_embedding_space_id"] == nil || fields["role"] == nil || fields["inputs"] == nil {
		return false
	}
	var body struct {
		Space  string            `json:"expected_embedding_space_id"`
		Role   string            `json:"role"`
		Inputs []json.RawMessage `json:"inputs"`
	}
	if json.Unmarshal(raw, &body) != nil || body.Role != "query" || !strings.HasPrefix(body.Space, "es_") || !chunking.ValidDigest(strings.TrimPrefix(body.Space, "es_")) || len(body.Inputs) != 1 {
		return false
	}
	var inputFields map[string]json.RawMessage
	if chunking.Decode(body.Inputs[0], &inputFields) != nil || len(inputFields) != 2 || inputFields["id"] == nil || inputFields["text"] == nil {
		return false
	}
	var input struct {
		ID   string `json:"id"`
		Text string `json:"text"`
	}
	return json.Unmarshal(body.Inputs[0], &input) == nil && queryInputID.MatchString(input.ID) && strings.TrimSpace(input.Text) != "" && len(input.Text) <= 128<<10
}

func (s *Server) consoleModelRoutes(mux *http.ServeMux) {
	// Only fixed Model API resources are exposed. No deployment lifecycle routes.
	mux.HandleFunc("GET /console/model/deployments", func(w http.ResponseWriter, r *http.Request) {
		q, err := url.ParseQuery(r.URL.RawQuery)
		if err != nil {
			fail(w, status.Error(codes.InvalidArgument, "invalid model list query"))
			return
		}
		for key, values := range q {
			if (key != "cursor" && key != "limit") || len(values) != 1 {
				fail(w, status.Error(codes.InvalidArgument, "only one cursor and limit are allowed"))
				return
			}
		}
		n, err := limit(r)
		if err != nil || n > 20 || len(q.Get("cursor")) > 2048 {
			fail(w, status.Error(codes.InvalidArgument, "model list limit must be 1..20 and cursor bounded"))
			return
		}
		q.Set("limit", fmt.Sprint(n))
		q.Set("status", "ready")
		s.forwardModel(w, r, "GET", "/api/deployments?"+q.Encode(), nil)
	})
	for _, resource := range []string{"model-versions", "models"} {
		resource := resource
		mux.HandleFunc("GET /console/model/"+resource+"/{id}", func(w http.ResponseWriter, r *http.Request) {
			if !retrieval.ValidID(r.PathValue("id")) || r.URL.RawQuery != "" {
				fail(w, status.Error(codes.InvalidArgument, "valid model UUID and no query required"))
				return
			}
			s.forwardModel(w, r, "GET", "/api/"+resource+"/"+r.PathValue("id"), nil)
		})
	}

	for _, route := range []struct{ method, suffix string }{{"GET", "embedding-profile"}, {"GET", "reranker-profile"}, {"POST", "embeddings"}} {
		method, suffix := route.method, route.suffix
		mux.HandleFunc(method+" /console/model/deployments/{id}/"+suffix, func(w http.ResponseWriter, r *http.Request) {
			if s.ModelConsole == nil {
				fail(w, status.Error(codes.Unavailable, "Model Gateway connection is not configured"))
				return
			}
			if !retrieval.ValidID(r.PathValue("id")) || r.URL.RawQuery != "" {
				fail(w, status.Error(codes.InvalidArgument, "Model deployment UUID and no query parameters required"))
				return
			}
			var raw []byte
			var err error
			if method == "POST" {
				raw, err = io.ReadAll(http.MaxBytesReader(w, r.Body, 256<<10))
				if err != nil {
					fail(w, status.Error(codes.ResourceExhausted, "query request is too large"))
					return
				}
				if !validateConsoleQuery(raw) {
					fail(w, status.Error(codes.InvalidArgument, "one raw query input and expected embedding space are required"))
					return
				}
			}
			s.forwardModel(w, r, method, "/api/deployments/"+r.PathValue("id")+"/"+suffix, raw)
		})
	}
}

func (s *Server) forwardModel(w http.ResponseWriter, r *http.Request, method, path string, raw []byte) {
	if s.ModelConsole == nil {
		fail(w, status.Error(codes.Unavailable, "Model Gateway connection is not configured"))
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 90*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, method, s.ModelConsole.base+path, bytes.NewReader(raw))
	if err != nil {
		fail(w, status.Error(codes.Internal, "Model request could not be formed"))
		return
	}
	req.Header.Set("Content-Type", "application/json")
	res, err := s.ModelConsole.client.Do(req)
	if err != nil {
		if ctx.Err() != nil {
			fail(w, status.FromContextError(ctx.Err()).Err())
		} else {
			fail(w, status.Error(codes.Unavailable, "Model Gateway request failed"))
		}
		return
	}
	defer res.Body.Close()
	body, err := io.ReadAll(io.LimitReader(res.Body, (1<<20)+1))
	if err != nil || len(body) > 1<<20 || !json.Valid(body) || res.StatusCode < 200 || res.StatusCode >= 600 {
		fail(w, status.Error(codes.DataLoss, "Model Gateway returned an invalid response"))
		return
	}
	writeRaw(w, res.StatusCode, body)
}
