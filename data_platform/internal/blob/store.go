// Package blob confines writes to this platform's source-document bucket.
package blob

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"regexp"

	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type Store interface {
	Put(context.Context, string, io.Reader, int64, string) error
	Open(context.Context, string) (io.ReadCloser, error)
	URI(string) string
}
type MinIO struct {
	client *minio.Client
	bucket string
}

var digestPattern = regexp.MustCompile(`^[a-f0-9]{64}$`)

func ValidDigest(s string) bool { return digestPattern.MatchString(s) }
func Key(runID, digest string) (string, error) {
	id, e := uuid.Parse(runID)
	if e != nil || id == uuid.Nil || id.String() != runID || !ValidDigest(digest) {
		return "", fmt.Errorf("invalid source identity")
	}
	return "sources/" + runID + "/sha256/" + digest + "/original", nil
}

// Browser response evidence has a distinct key even when bytes equal the DOM.
func BrowserResponseKey(runID, sourceURL, digest string) (string, error) {
	key, e := CrawlItemKey(runID, sourceURL, digest)
	if e != nil {
		return "", e
	}
	return key + ".http-response", nil
}

// Upload keys remain stable on retries even if a database commit failed.
// Separate namespaces preserve existing crawl object identities.
func UploadKey(keyHash, digest string) (string, error) {
	if !ValidDigest(keyHash) || !ValidDigest(digest) {
		return "", fmt.Errorf("invalid upload identity")
	}
	return "uploads/" + keyHash + "/sha256/" + digest + "/original", nil
}
func Connect(ctx context.Context, endpoint, key, secret, bucket string, secure bool) (*MinIO, error) {
	// Never silently reuse the model platform's artifact or shared buckets.
	if bucket != "data-sources" {
		return nil, fmt.Errorf("DATA_SOURCE_BUCKET must be data-sources in this release")
	}
	return connect(ctx, endpoint, key, secret, bucket, secure)
}

// ConnectCanonical is explicit so Collection cannot be configured to write derived results.
func ConnectCanonical(ctx context.Context, endpoint, key, secret string, secure bool) (*MinIO, error) {
	return connect(ctx, endpoint, key, secret, "data-canonical", secure)
}
func connect(ctx context.Context, endpoint, key, secret, bucket string, secure bool) (*MinIO, error) {
	c, e := minio.New(endpoint, &minio.Options{Creds: credentials.NewStaticV4(key, secret, ""), Secure: secure})
	if e != nil {
		return nil, e
	}
	exists, e := c.BucketExists(ctx, bucket)
	if e != nil {
		return nil, e
	}
	if !exists {
		if e = c.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); e != nil {
			present, check := c.BucketExists(ctx, bucket)
			if check != nil || !present {
				return nil, e
			}
		}
	}
	return &MinIO{client: c, bucket: bucket}, nil
}
func (s *MinIO) URI(key string) string { return "s3://" + s.bucket + "/" + key }
func (s *MinIO) Open(ctx context.Context, key string) (io.ReadCloser, error) {
	return s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
}
func (s *MinIO) Put(ctx context.Context, key string, r io.Reader, size int64, kind string) error {
	_, e := s.client.PutObject(ctx, s.bucket, key, r, size, minio.PutObjectOptions{ContentType: kind})
	return e
}
func Verify(ctx context.Context, s Store, key, digest string, size int64) error {
	if size < 0 || size > 32<<20 || !ValidDigest(digest) {
		return fmt.Errorf("invalid expected content")
	}
	r, e := s.Open(ctx, key)
	if e != nil {
		return e
	}
	defer r.Close()
	h := sha256.New()
	n, e := io.Copy(h, io.LimitReader(r, size+1))
	if e != nil {
		return e
	}
	if n != size || hex.EncodeToString(h.Sum(nil)) != digest {
		return fmt.Errorf("stored source checksum mismatch")
	}
	return nil
}

// URL-addressed batch keys preserve distinct provenance for identical content
// fetched from different links in the same crawl. Existing single keys stay put.
func CrawlItemKey(runID, sourceURL, digest string) (string, error) {
	if _, err := Key(runID, digest); err != nil {
		return "", err
	}
	sum := sha256.Sum256([]byte(sourceURL))
	return "sources/" + runID + "/urls/" + hex.EncodeToString(sum[:]) + "/sha256/" + digest + "/original", nil
}
