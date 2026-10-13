// Package browser drives an offline Chromium child via a bounded pipe protocol.
// Public HTTP GETs run here, using crawl's checked-address dialer, never in Chromium.
package browser

import (
	"context"
	"data-platform/internal/crawl"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
)

type reply struct {
	Status  int               `json:"status,omitempty"`
	Headers map[string]string `json:"headers,omitempty"`
	Body    []byte            `json:"body"`
	Blocked bool              `json:"blocked,omitempty"`
	Error   bool              `json:"error,omitempty"`
}

func headers(h http.Header) map[string]string {
	out := map[string]string{}
	for _, k := range []string{"Content-Type", "Content-Security-Policy", "Content-Security-Policy-Report-Only", "X-Content-Type-Options", "Access-Control-Allow-Origin", "Access-Control-Allow-Methods", "Access-Control-Allow-Headers", "Cross-Origin-Resource-Policy", "Cross-Origin-Opener-Policy", "Cross-Origin-Embedder-Policy", "Referrer-Policy"} {
		if value := h.Values(k); len(value) > 0 {
			out[strings.ToLower(k)] = strings.Join(value, ", ")
		}
	}
	// No cookies, auth challenges, refresh, content encoding or reporting endpoints.
	return out
}

type networkBudget struct {
	requests int
	bytes    int64
}

func (r *Renderer) fetch(ctx context.Context, f *crawl.Fetcher, seed, raw, method string, o crawl.Options, page *networkBudget) (reply, error) {
	normalized, e := crawl.NormalizeURL(raw)
	if e != nil || method != "GET" || !crawl.InScope(seed, normalized) {
		return reply{Blocked: true}, nil
	}
	if page.requests >= crawl.BrowserPageRequests || r.budget.requests >= crawl.BrowserRunRequests {
		return reply{}, fmt.Errorf("browser request limit exceeded")
	}
	remaining := min(crawl.BrowserPageBytes-page.bytes, crawl.BrowserRunBytes-r.budget.bytes, o.MaxBytes)
	if remaining <= 0 {
		return reply{}, fmt.Errorf("browser network byte limit exceeded")
	}
	page.requests++
	r.budget.requests++
	// One hop per browser request. Chromium observes the redirect and resolves
	// relative module/CSS URLs using the correct final URL. Each hop is checked.
	client := *f.Client
	client.CheckRedirect = func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }
	req, e := http.NewRequestWithContext(ctx, "GET", normalized, nil)
	if e != nil {
		return reply{}, e
	}
	req.Header.Set("User-Agent", "DataPlatformCollector/1.0")
	req.Header.Set("Accept-Encoding", "identity")
	resp, e := client.Do(req)
	if e != nil {
		return reply{}, e
	}
	defer resp.Body.Close()
	if enc := resp.Header.Get("Content-Encoding"); enc != "" && enc != "identity" {
		return reply{}, fmt.Errorf("compressed browser resource rejected")
	}
	if resp.ContentLength > remaining {
		return reply{}, fmt.Errorf("browser resource limit exceeded")
	}
	body, e := io.ReadAll(io.LimitReader(resp.Body, remaining+1))
	if e != nil || int64(len(body)) > remaining || (resp.ContentLength >= 0 && resp.ContentLength != int64(len(body))) {
		return reply{}, fmt.Errorf("incomplete or oversized browser resource")
	}
	page.bytes += int64(len(body))
	r.budget.bytes += int64(len(body))
	out := reply{Status: resp.StatusCode, Headers: headers(resp.Header), Body: body}
	switch resp.StatusCode {
	case 301, 302, 303, 307, 308:
		location, e := url.Parse(resp.Header.Get("Location"))
		if e != nil || location.String() == "" {
			return reply{}, fmt.Errorf("invalid browser redirect")
		}
		target, e := crawl.NormalizeURL(req.URL.ResolveReference(location).String())
		if e != nil || !crawl.InScope(seed, target) || !crawl.InScope(normalized, target) {
			return reply{Blocked: true}, nil
		}
		out.Headers["location"] = target
	default:
		if resp.StatusCode < 200 || resp.StatusCode >= 300 || resp.StatusCode == 206 {
			return reply{}, fmt.Errorf("browser resource HTTP failure")
		}
	}
	return out, nil
}
