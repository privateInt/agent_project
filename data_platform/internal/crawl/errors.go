package crawl

import (
	"context"
	"errors"
	"fmt"
)

type HTTPStatusError struct{ Status int }

func (e *HTTPStatusError) Error() string { return fmt.Sprintf("HTTP response status %d", e.Status) }
func PublicError(err error) string {
	var httpErr *HTTPStatusError
	if errors.As(err, &httpErr) {
		return httpErr.Error()
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "Source download exceeded its timeout"
	}
	switch err.Error() {
	case "source exceeds max_total_bytes", "page link returned an attachment while include_attachments is disabled", "HTML link extraction exceeded its limits or failed", "source exceeds max_bytes", "incomplete HTTP response", "unexpected compressed response":
		return err.Error()
	}
	// Wrapped net/url errors may contain query credentials. They never enter the
	// public Operation response or the Job's ordinary log output.
	return "Source download failed: check public URL access, redirects, DNS and TLS"
}
