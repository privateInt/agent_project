package main

import (
	"context"
	"errors"
	"io"
	"net/http"
	"os"
	"strings"
	"testing"
	"time"
)

func TestHF429RespectsServerResetAndDate(t *testing.T) {
	now := time.Date(2026, 9, 15, 0, 0, 0, 0, time.UTC)
	for _, c := range []struct {
		name string
		head http.Header
		want time.Duration
	}{
		{"retry-seconds", http.Header{"Retry-After": {"12"}}, 13 * time.Second},
		{"rate-reset", http.Header{"Ratelimit": {`"resolvers";r=0;t=299`}}, 300 * time.Second},
		{"both", http.Header{"Retry-After": {"1"}, "Ratelimit": {`"api";r=0;t=5, "resolvers";r=0;t=20`}}, 21 * time.Second},
		{"server-clock", http.Header{"Date": {now.Format(http.TimeFormat)}, "Retry-After": {now.Add(60 * time.Second).Format(http.TimeFormat)}}, 61 * time.Second},
		{"missing", http.Header{}, 301 * time.Second},
		{"invalid", http.Header{"Retry-After": {"-2"}, "Ratelimit": {`"resolvers";t=oops`}}, 301 * time.Second},
		{"overflow", http.Header{"Retry-After": {"999999999999999999999999"}}, maxHFWait + 2*time.Second},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := hfRetryDelay(c.head, now.Add(time.Hour)); got != c.want {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
}

type closeTrackingBody struct {
	io.Reader
	closed bool
}

func (b *closeTrackingBody) Close() error { b.closed = true; return nil }

func TestHF429ClosesResponseThenRetriesOnlyTheGET(t *testing.T) {
	first := &closeTrackingBody{Reader: strings.NewReader("limit")}
	calls, waits := 0, 0
	getter := hfGetter{now: time.Now}
	getter.client = &http.Client{Transport: transferTransport(func(r *http.Request) (*http.Response, error) {
		calls++
		if r.Method != "GET" || r.Header.Get("Authorization") != "Bearer test-only-token" || r.URL.Path != "/fixed/resolve/commit/model.safetensors" {
			t.Fatal("request identity changed")
		}
		if calls == 1 {
			return &http.Response{StatusCode: 429, Header: http.Header{"Ratelimit": {`"resolvers";r=0;t=37`}}, Body: first}, nil
		}
		return &http.Response{StatusCode: 200, Body: io.NopCloser(strings.NewReader("weights"))}, nil
	})}
	getter.wait = func(_ context.Context, delay time.Duration) error {
		if !first.closed || delay != 38*time.Second {
			t.Fatal("response not closed or reset ignored", delay)
		}
		waits++
		return nil
	}
	res, err := getter.get(context.Background(), "https://huggingface.co/fixed/resolve/commit/model.safetensors", "test-only-token")
	if err != nil {
		t.Fatal(err)
	}
	defer res.Body.Close()
	body, _ := io.ReadAll(res.Body)
	if calls != 2 || waits != 1 || string(body) != "weights" {
		t.Fatal(calls, waits, string(body))
	}
}

func TestHF429RetryBudgetAndCancellation(t *testing.T) {
	for _, c := range []struct {
		name, header string
		cancel       bool
		calls, waits int
	}{
		{"attempts", "1", false, 4, 3},
		{"server-delay-too-long", "9999", false, 1, 0},
		{"total-wait", "700", false, 2, 1},
		{"cancelled", "30", true, 1, 1},
	} {
		t.Run(c.name, func(t *testing.T) {
			calls, waits := 0, 0
			var bodies []*closeTrackingBody
			getter := hfGetter{now: time.Now, client: &http.Client{Transport: transferTransport(func(*http.Request) (*http.Response, error) {
				calls++
				body := &closeTrackingBody{Reader: strings.NewReader("limit")}
				bodies = append(bodies, body)
				return &http.Response{StatusCode: 429, Header: http.Header{"Retry-After": {c.header}}, Body: body}, nil
			})}}
			getter.wait = func(ctx context.Context, _ time.Duration) error {
				waits++
				if c.cancel {
					return context.Canceled
				}
				return nil
			}
			_, err := getter.get(context.Background(), "https://huggingface.co/example", "")
			if err == nil || calls != c.calls || waits != c.waits {
				t.Fatal(err, calls, waits)
			}
			for _, body := range bodies {
				if !body.closed {
					t.Fatal("429 body leaked")
				}
			}
		})
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := waitHF(ctx, time.Hour); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
}

func TestHFDoesNotRetryAuthenticationNetworkOrOtherStatus(t *testing.T) {
	for _, code := range []int{200, 401, 403, 404, 500, 503, 0} {
		calls := 0
		getter := hfGetter{now: time.Now, client: &http.Client{Transport: transferTransport(func(*http.Request) (*http.Response, error) {
			calls++
			if code == 0 {
				return nil, errors.New("connection failed")
			}
			return &http.Response{StatusCode: code, Body: io.NopCloser(strings.NewReader("body"))}, nil
		})}, wait: func(context.Context, time.Duration) error { t.Fatal("unexpected retry", code); return nil }}
		res, err := getter.get(context.Background(), "https://huggingface.co/example", "")
		if code == 0 && err == nil || code != 0 && (err != nil || res.StatusCode != code) || calls != 1 {
			t.Fatal(code, err, calls)
		}
		if res != nil {
			res.Body.Close()
		}
	}
}

type failedTransferBody struct{}

func (failedTransferBody) Read([]byte) (int, error) { return 0, io.ErrUnexpectedEOF }
func (failedTransferBody) Close() error             { return nil }

func TestHFPartialOrFailedBodyNeverBecomesStoredFile(t *testing.T) {
	for _, code := range []int{206, 200} {
		old := http.DefaultClient
		t.Cleanup(func() { http.DefaultClient = old })
		directory := t.TempDir()
		t.Setenv("TMPDIR", directory)
		calls := 0
		http.DefaultClient = &http.Client{Transport: transferTransport(func(*http.Request) (*http.Response, error) {
			calls++
			return &http.Response{StatusCode: code, ContentLength: 7, Body: failedTransferBody{}}, nil
		})}
		store := &memoryTransferStore{}
		_, _, err := transferFiles(context.Background(), store, "example/repo", "commit", []struct {
			Filename string `json:"rfilename"`
		}{{"model.safetensors"}}, "prefix/", "", nil)
		files, _ := os.ReadDir(directory)
		if err == nil || calls != 1 || store.payload != nil || len(files) != 0 {
			t.Fatal("failed download retried or stored", err, calls, files)
		}
	}
}
