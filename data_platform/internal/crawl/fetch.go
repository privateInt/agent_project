package crawl

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"mime"
	"net/http"
	"os"
	"time"
)

const ProcessorVersion = "http-source-v1"

type Options struct {
	MaxBytes           int64 `json:"max_bytes"`
	TimeoutSeconds     int32 `json:"timeout_seconds"`
	FollowLinks        bool  `json:"follow_links,omitempty"`
	IncludeAttachments bool  `json:"include_attachments,omitempty"`
	MaxDepth           int32 `json:"max_depth,omitempty"`
	MaxDocuments       int32 `json:"max_documents,omitempty"`
	MaxTotalBytes      int64 `json:"max_total_bytes,omitempty"`
	RenderJavaScript   bool  `json:"render_javascript,omitempty"`
	RenderWaitMS       int32 `json:"render_wait_ms,omitempty"`
}

func (o Options) Expanded() bool { return o.FollowLinks || o.IncludeAttachments || o.RenderJavaScript }
func NormalizeOptions(o Options) (Options, error) {
	if o.MaxBytes == 0 {
		o.MaxBytes = 8 << 20
	}
	if o.TimeoutSeconds == 0 {
		o.TimeoutSeconds = 120
	}
	if o.MaxBytes < 1 || o.MaxBytes > 32<<20 || o.TimeoutSeconds < 5 || o.TimeoutSeconds > 300 {
		return o, fmt.Errorf("max_bytes must be 1..33554432 and timeout_seconds 5..300")
	}
	if o.RenderJavaScript {
		if o.RenderWaitMS == 0 {
			o.RenderWaitMS = 1000
		}
		if o.RenderWaitMS < 1 || o.RenderWaitMS > 10000 {
			return o, fmt.Errorf("render_wait_ms must be 1..10000")
		}
	} else if o.RenderWaitMS != 0 {
		return o, fmt.Errorf("render_wait_ms requires render_javascript")
	}
	if !o.Expanded() {
		if o.MaxDepth != 0 || o.MaxDocuments != 0 || o.MaxTotalBytes != 0 {
			return o, fmt.Errorf("scope limits require follow_links or include_attachments")
		}
		return o, nil
	}
	if o.FollowLinks && o.MaxDepth == 0 {
		o.MaxDepth = 1
	}
	if (!o.FollowLinks && o.MaxDepth != 0) || o.MaxDepth < 0 || o.MaxDepth > 3 {
		return o, fmt.Errorf("max_depth must be 1..3 for follow_links, otherwise 0")
	}
	if o.MaxDocuments == 0 {
		o.MaxDocuments = 10
		if o.RenderJavaScript && !o.FollowLinks && !o.IncludeAttachments {
			o.MaxDocuments = 1
		}
	}
	if o.MaxDocuments < 1 || o.MaxDocuments > 20 {
		return o, fmt.Errorf("max_documents must be 1..20 including the seed")
	}
	if o.MaxTotalBytes == 0 {
		o.MaxTotalBytes = min(o.MaxBytes*int64(o.MaxDocuments)*o.ArtifactMultiplier(), int64(64<<20))
	}
	if o.MaxTotalBytes < o.MaxBytes || o.MaxTotalBytes > 64<<20 {
		return o, fmt.Errorf("max_total_bytes must be max_bytes..67108864")
	}
	return o, nil
}

type Result struct {
	Path, FinalURL, SHA256, ContentType string
	Size                                int64
	Headers                             http.Header
	Status                              int
}
type Fetcher struct{ Client *http.Client }

func NewFetcher() *Fetcher {
	tr := &http.Transport{Proxy: nil, DialContext: publicDial, DisableCompression: true, DisableKeepAlives: true,
		TLSHandshakeTimeout: 15 * time.Second, ResponseHeaderTimeout: 30 * time.Second, MaxResponseHeaderBytes: 1 << 20, ForceAttemptHTTP2: false}
	return &Fetcher{Client: &http.Client{Transport: tr, CheckRedirect: func(req *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return fmt.Errorf("redirect limit exceeded")
		}
		_, e := NormalizeURL(req.URL.String())
		if e != nil {
			return e
		}
		if len(via) > 0 && via[len(via)-1].URL.Scheme == "https" && req.URL.Scheme != "https" {
			return fmt.Errorf("HTTPS downgrade redirect is not permitted")
		}
		return nil
	}}}
}
func (f *Fetcher) Fetch(ctx context.Context, rawURL string, o Options) (result Result, err error) {
	normalized, e := NormalizeURL(rawURL)
	if e != nil {
		return result, e
	}
	o, e = NormalizeOptions(o)
	if e != nil {
		return result, e
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.TimeoutSeconds)*time.Second)
	defer cancel()
	req, e := http.NewRequestWithContext(ctx, http.MethodGet, normalized, nil)
	if e != nil {
		return result, e
	}
	req.Header.Set("User-Agent", "DataPlatformCollector/1.0")
	req.Header.Set("Accept-Encoding", "identity")
	resp, e := f.Client.Do(req)
	if e != nil {
		return result, fmt.Errorf("HTTP request failed: %w", e)
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 || resp.StatusCode == http.StatusPartialContent {
		return result, &HTTPStatusError{Status: resp.StatusCode}
	}
	if resp.Header.Get("Content-Encoding") != "" && resp.Header.Get("Content-Encoding") != "identity" {
		return result, fmt.Errorf("unexpected compressed response")
	}
	if resp.ContentLength > o.MaxBytes {
		return result, fmt.Errorf("source exceeds max_bytes")
	}
	file, e := os.CreateTemp("", "data-source-*")
	if e != nil {
		return result, e
	}
	defer func() {
		file.Close()
		if err != nil {
			os.Remove(file.Name())
		}
	}()
	h := sha256.New()
	n, e := io.Copy(io.MultiWriter(file, h), io.LimitReader(resp.Body, o.MaxBytes+1))
	if e != nil {
		return result, e
	}
	if n > o.MaxBytes {
		return result, fmt.Errorf("source exceeds max_bytes")
	}
	if resp.ContentLength >= 0 && resp.ContentLength != n {
		return result, fmt.Errorf("incomplete HTTP response")
	}
	if e = file.Close(); e != nil {
		return result, e
	}
	ct, params, e := mime.ParseMediaType(resp.Header.Get("Content-Type"))
	if e != nil || ct == "" || len(resp.Header.Get("Content-Type")) > 512 {
		ct = "application/octet-stream"
		params = nil
	}
	ct = mime.FormatMediaType(ct, params)
	final, e := NormalizeURL(resp.Request.URL.String())
	if e != nil {
		return result, e
	}
	return Result{Path: file.Name(), FinalURL: final, SHA256: hex.EncodeToString(h.Sum(nil)), ContentType: ct, Size: n, Status: resp.StatusCode, Headers: resp.Header.Clone()}, nil
}
