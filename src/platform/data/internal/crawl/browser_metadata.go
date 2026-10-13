package crawl

import (
	collectionv1 "data-platform/gen/collection/v1"
	"data-platform/internal/blob"
	"fmt"
	"mime"
)

const BrowserPageRequests = 128
const BrowserPageBytes int64 = 32 << 20
const BrowserRunRequests = 512
const BrowserRunBytes int64 = 64 << 20

// Metadata is absent for byte-preserving HTTP results. A rendered HTML page
// must retain a separately verified initial HTTP response, including its type.
func ValidateCapture(o Options, role, contentType string, c *collectionv1.BrowserCapture) error {
	bad := func() error { return fmt.Errorf("invalid browser capture provenance") }
	expected := o.RenderJavaScript && role != "attachment"
	if !expected {
		if c != nil {
			return bad()
		}
		return nil
	}
	if c == nil || c.Representation != "rendered_dom" || contentType != "text/html; charset=utf-8" || !blob.ValidDigest(c.ResponseSha256) || c.ResponseSizeBytes < 0 || c.ResponseSizeBytes > o.MaxBytes || !IsHTML(c.ResponseContentType) {
		return bad()
	}
	k, p, e := mime.ParseMediaType(c.ResponseContentType)
	if e != nil || mime.FormatMediaType(k, p) != c.ResponseContentType || len(c.ResponseContentType) > 512 {
		return bad()
	}
	if c.NetworkRequests < 1 || c.NetworkRequests > BrowserPageRequests || c.NetworkBytes < c.ResponseSizeBytes || c.NetworkBytes > BrowserPageBytes || c.BlockedRequests < 0 || c.BlockedRequests > 1024 {
		return bad()
	}
	return nil
}
