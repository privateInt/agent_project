package main

import (
	"context"
	"fmt"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

const maxHFWait = 20 * time.Minute
const maxHFRetries = 3

var resetSeconds = regexp.MustCompile(`(?:^|[;,])\s*t\s*=\s*([0-9]+)(?:\s*[,;]|\s*$)`)

// Only retry an explicit 429 response to GET, before consuming a file body.
// Never retry accepted/partial transfers, storage writes or a failed Operation.
type hfGetter struct {
	client *http.Client
	now    func() time.Time
	wait   func(context.Context, time.Duration) error
}

func waitHF(ctx context.Context, delay time.Duration) error {
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func hfSeconds(value string) time.Duration {
	seconds, err := strconv.ParseUint(value, 10, 64)
	if err != nil || seconds > uint64(maxHFWait/time.Second) {
		return maxHFWait + time.Second
	}
	return time.Duration(seconds) * time.Second
}

func hfRetryDelay(header http.Header, now time.Time) time.Duration {
	var delay time.Duration
	found := false
	if value := strings.TrimSpace(header.Get("Retry-After")); value != "" {
		if _, err := strconv.ParseUint(value, 10, 64); err == nil || strings.Trim(value, "0123456789") == "" {
			delay, found = hfSeconds(value), true
		} else if until, err := http.ParseTime(value); err == nil {
			// Use the response clock when present, avoiding a fast client clock.
			if server, err := http.ParseTime(header.Get("Date")); err == nil {
				now = server
			}
			delay, found = until.Sub(now), true
		}
	}
	for _, value := range header.Values("RateLimit") {
		for _, match := range resetSeconds.FindAllStringSubmatch(value, -1) {
			candidate := hfSeconds(match[1])
			if candidate > delay {
				delay = candidate
			}
			found = true
		}
	}
	if !found {
		// HF documents five-minute quota windows. Avoid immediate blind retries.
		delay = 5 * time.Minute
	}
	if delay < 0 {
		delay = 0
	}
	return delay + time.Second
}

func (g hfGetter) get(ctx context.Context, endpoint, token string) (*http.Response, error) {
	var waited time.Duration
	for retry := 0; ; retry++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
		if err != nil {
			return nil, err
		}
		setHFHeaders(req, token)
		res, err := g.client.Do(req)
		if err != nil {
			return nil, err
		}
		if res.StatusCode != http.StatusTooManyRequests {
			return res, nil
		}
		delay := hfRetryDelay(res.Header, g.now())
		res.Body.Close()
		if retry >= maxHFRetries || delay > maxHFWait-waited {
			return nil, fmt.Errorf("status 429: Hugging Face retry budget exhausted; create a new import after the limit resets")
		}
		log.Printf("huggingface_rate_limited retry=%d/%d wait_seconds=%.0f", retry+1, maxHFRetries, delay.Seconds())
		if err := g.wait(ctx, delay); err != nil {
			return nil, err
		}
		waited += delay
	}
}

func getHF(ctx context.Context, endpoint, token string) (*http.Response, error) {
	return (hfGetter{client: http.DefaultClient, now: time.Now, wait: waitHF}).get(ctx, endpoint, token)
}
