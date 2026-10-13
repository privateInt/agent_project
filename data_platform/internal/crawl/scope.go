package crawl

import (
	"context"
	collectionv1 "data-platform/gen/collection/v1"
	"fmt"
	"io"
	"mime"
	"net/http"
	"net/url"
	"os"
	"path"
	"strings"
	"time"

	"golang.org/x/net/html"
	"golang.org/x/net/html/charset"
)

const ScopeProcessorVersion = "http-scope-v1"
const MaxPageLinks = 256

type Link struct {
	URL        string
	Attachment bool
}
type Item struct {
	Result
	SourceURL, ParentURL, Role string
	Response                   *Result
	Capture                    *collectionv1.BrowserCapture
	Depth                      int32
}
type Batch struct {
	Items                                                     []Item
	DocumentLimitReached, DepthLimitReached, LinkLimitReached bool
}

// Same hostname, including the seed, at EVERY redirect hop. A subdomain is a
// different host. HTTPS cannot downgrade even on the same host.
func InScope(seed, target string) bool {
	a, e := url.Parse(seed)
	if e != nil {
		return false
	}
	b, e := url.Parse(target)
	if e != nil {
		return false
	}
	return a.Hostname() == b.Hostname() && (a.Scheme != "https" || b.Scheme == "https")
}
func (f *Fetcher) scoped(seed string) *Fetcher {
	client := *f.Client
	previous := client.CheckRedirect
	client.CheckRedirect = func(r *http.Request, via []*http.Request) error {
		if len(via) >= 10 {
			return fmt.Errorf("redirect limit exceeded")
		}
		normalized, e := NormalizeURL(r.URL.String())
		if e != nil {
			return e
		}
		if !InScope(seed, normalized) {
			return fmt.Errorf("redirect left requested host")
		}
		if len(via) > 0 && via[len(via)-1].URL.Scheme == "https" && r.URL.Scheme != "https" {
			return fmt.Errorf("HTTPS downgrade redirect is not permitted")
		}
		if previous != nil {
			return previous(r, via)
		}
		return nil
	}
	return &Fetcher{Client: &client}
}
func IsHTML(contentType string) bool {
	k, _, e := mime.ParseMediaType(contentType)
	return e == nil && k == "text/html"
}

// Inspect a decoding view for links; the stored source bytes remain untouched.
// Only <a href> links are eligible: no scripts, forms, frames or image resources.
func Discover(r io.Reader, contentType, finalURL, seed string) ([]Link, bool, error) {
	decoded, e := charset.NewReader(r, contentType)
	if e != nil {
		return nil, false, fmt.Errorf("HTML charset could not be decoded")
	}
	z := html.NewTokenizer(decoded)
	z.SetMaxBuf(1 << 20)
	type anchor struct {
		href     string
		download bool
	}
	anchors := []anchor{}
	base, _ := url.Parse(finalURL)
	baseSeen := false
	limited := false
	for {
		tt := z.Next()
		if tt == html.ErrorToken {
			if z.Err() != io.EOF {
				return nil, false, fmt.Errorf("HTML link extraction exceeded its limits or failed")
			}
			break
		}
		if tt != html.StartTagToken && tt != html.SelfClosingTagToken {
			continue
		}
		token := z.Token()
		if token.Data != "a" && token.Data != "base" {
			continue
		}
		href := ""
		hasHref := false
		download := false
		for _, a := range token.Attr {
			if a.Key == "href" && !hasHref {
				href = strings.TrimSpace(a.Val)
				hasHref = true
			}
			if a.Key == "download" {
				download = true
			}
		}
		if token.Data == "base" && !baseSeen && hasHref {
			baseSeen = true
			if u, e := url.Parse(href); e == nil {
				base = base.ResolveReference(u)
			}
			continue
		}
		if token.Data != "a" || !hasHref || href == "" {
			continue
		}
		if len(anchors) >= MaxPageLinks {
			limited = true
			continue
		}
		anchors = append(anchors, anchor{href, download})
	}
	out := []Link{}
	seen := map[string]bool{}
	for _, a := range anchors {
		if strings.HasPrefix(a.href, "#") {
			continue
		}
		ref, e := url.Parse(a.href)
		if e != nil {
			continue
		}
		raw, e := NormalizeURL(base.ResolveReference(ref).String())
		if e != nil || !InScope(seed, raw) || !InScope(finalURL, raw) || seen[raw] {
			continue
		}
		u, _ := url.Parse(raw)
		ext := strings.ToLower(path.Ext(u.Path))
		attachment := a.download
		switch ext {
		case ".pdf", ".docx", ".xlsx", ".txt", ".md", ".png", ".jpg", ".jpeg":
			attachment = true
		case "", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp":
		default:
			if !attachment {
				continue
			}
		}
		seen[raw] = true
		out = append(out, Link{raw, attachment})
	}
	return out, limited, nil
}

// Collect performs serial, paced GETs within one overall download/upload budget.
// The sink uploads one file at a time, so /tmp never contains the full batch.
// No source document is committed here: callers register the complete manifest
// atomically only after every selected download and upload succeeds.
func (f *Fetcher) Collect(ctx context.Context, seed string, o Options, sink func(context.Context, Item) error) (batch Batch, err error) {
	return f.CollectRendered(ctx, seed, o, sink, nil)
}

// RenderFunc returns a bounded DOM file and metrics. Collect retains and cleans
// up both files; the sink persists both before the atomic completion request.
type RenderFunc func(context.Context, *Fetcher, string, Options, Result) (Result, *collectionv1.BrowserCapture, error)

func (f *Fetcher) CollectRendered(ctx context.Context, seed string, o Options, sink func(context.Context, Item) error, render RenderFunc) (batch Batch, err error) {
	o, e := NormalizeOptions(o)
	if o.RenderJavaScript && render == nil {
		return batch, fmt.Errorf("browser renderer unavailable")
	}
	if e != nil {
		return batch, e
	}
	if !o.Expanded() {
		return batch, fmt.Errorf("expanded collection options required")
	}
	seed, e = NormalizeURL(seed)
	if e != nil {
		return batch, e
	}
	ctx, cancel := context.WithTimeout(ctx, time.Duration(o.TimeoutSeconds)*time.Second)
	defer cancel()
	fetcher := f.scoped(seed)
	type candidate struct {
		url, parent, role string
		depth             int32
	}
	queue := []candidate{{url: seed, role: "seed"}}
	scheduled := map[string]bool{seed: true}
	var total int64
	for len(queue) > 0 {
		if e := ctx.Err(); e != nil {
			return batch, e
		}
		if len(batch.Items) >= int(o.MaxDocuments) {
			batch.DocumentLimitReached = true
			break
		}
		next := queue[0]
		queue = queue[1:]
		if len(batch.Items) > 0 {
			timer := time.NewTimer(250 * time.Millisecond)
			select {
			case <-ctx.Done():
				timer.Stop()
				return batch, ctx.Err()
			case <-timer.C:
			}
		}
		remaining := o.MaxTotalBytes - total
		if remaining <= 0 {
			return batch, fmt.Errorf("source exceeds max_total_bytes")
		}
		result, e := fetcher.Fetch(ctx, next.url, Options{MaxBytes: min(o.MaxBytes, remaining), TimeoutSeconds: o.TimeoutSeconds})
		if e != nil {
			return batch, e
		}
		item := Item{Result: result, SourceURL: next.url, ParentURL: next.parent, Role: next.role, Depth: next.depth}
		e = func() error {
			defer os.Remove(result.Path)
			if !InScope(seed, result.FinalURL) {
				return fmt.Errorf("final response left requested host")
			}
			if o.RenderJavaScript && next.role != "attachment" && IsHTML(result.ContentType) {
				dom, capture, e := render(ctx, fetcher, seed, o, result)
				if e != nil {
					return e
				}
				defer os.Remove(dom.Path)
				if dom.Size+result.Size > remaining {
					return fmt.Errorf("source exceeds max_total_bytes")
				}
				initial := result
				item.Response, item.Capture = &initial, capture
				result = dom
				item.Result = dom
			} else if o.RenderJavaScript && next.role == "seed" {
				return fmt.Errorf("browser seed must be HTML")
			}
			isPage := IsHTML(result.ContentType)
			if next.role == "page" && !isPage {
				if !o.IncludeAttachments {
					return fmt.Errorf("page link returned an attachment while include_attachments is disabled")
				}
				item.Role = "attachment"
			}
			if isPage && next.role != "attachment" {
				file, e := os.Open(result.Path)
				if e != nil {
					return e
				}
				links, limited, e := Discover(file, result.ContentType, result.FinalURL, seed)
				file.Close()
				if e != nil {
					return e
				}
				batch.LinkLimitReached = batch.LinkLimitReached || limited
				scheduled[result.FinalURL] = true
				for _, link := range links {
					if scheduled[link.URL] {
						continue
					}
					role := "page"
					if link.Attachment {
						if !o.IncludeAttachments {
							continue
						}
						role = "attachment"
					} else {
						if !o.FollowLinks {
							continue
						}
						if next.depth >= o.MaxDepth {
							batch.DepthLimitReached = true
							continue
						}
					}
					if len(batch.Items)+1+len(queue) >= int(o.MaxDocuments) {
						batch.DocumentLimitReached = true
						continue
					}
					scheduled[link.URL] = true
					queue = append(queue, candidate{link.URL, result.FinalURL, role, next.depth + 1})
				}
			}
			if sink != nil {
				return sink(ctx, item)
			}
			return nil
		}()
		if e != nil {
			return batch, e
		}
		total += result.Size
		if item.Response != nil {
			total += item.Response.Size
			item.Response.Path = ""
		}
		item.Path = "" // Temporary files have been removed; only immutable metadata survives.
		batch.Items = append(batch.Items, item)
	}
	return batch, nil
}
