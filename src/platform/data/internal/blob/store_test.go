package blob

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"io"
	"strings"
	"testing"
)

type contentStore string

func (s contentStore) Put(context.Context, string, io.Reader, int64, string) error { return nil }
func (s contentStore) Open(context.Context, string) (io.ReadCloser, error) {
	return io.NopCloser(strings.NewReader(string(s))), nil
}
func (s contentStore) URI(k string) string { return k }
func TestStoredContentRequiresExactHashAndLength(t *testing.T) {
	sum := sha256.Sum256([]byte("abc"))
	digest := hex.EncodeToString(sum[:])
	for _, body := range []string{"ab", "abd", "abcd"} {
		if Verify(context.Background(), contentStore(body), "key", digest, 3) == nil {
			t.Fatal("tampered source accepted", body)
		}
	}
	if e := Verify(context.Background(), contentStore("abc"), "key", digest, 3); e != nil {
		t.Fatal(e)
	}
	if _, e := Key("../../outside", digest); e == nil {
		t.Fatal("unscoped key accepted")
	}
}

func TestBatchKeysSeparateSameBytesAtDifferentURLs(t *testing.T) {
	id := "11111111-1111-4111-8111-111111111111"
	digest := strings.Repeat("a", 64)
	a, e := CrawlItemKey(id, "https://example.com/a", digest)
	if e != nil {
		t.Fatal(e)
	}
	b, e := CrawlItemKey(id, "https://example.com/b", digest)
	if e != nil || a == b {
		t.Fatal("provenance collapsed", e)
	}
	old, _ := Key(id, digest)
	if a == old {
		t.Fatal("legacy object overwritten")
	}
	replay, _ := CrawlItemKey(id, "https://example.com/a", digest)
	if a != replay {
		t.Fatal("unstable key")
	}
}
