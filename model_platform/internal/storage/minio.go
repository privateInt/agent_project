package storage

import (
	"context"
	"fmt"
	"io"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type MinIO struct {
	client *minio.Client
	bucket string
}

func Connect(ctx context.Context, endpoint, accessKey, secretKey, bucket string, useSSL bool) (*MinIO, error) {
	store, exists, err := connectBucket(ctx, endpoint, accessKey, secretKey, bucket, useSSL)
	if err != nil {
		return nil, err
	}
	if !exists {
		if err := store.client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
			return nil, err
		}
	}
	return store, nil
}

// ConnectExisting connects to an input bucket without creating it. This avoids
// silently creating an empty bucket when a DatasetSpec contains a typo.
func ConnectExisting(ctx context.Context, endpoint, accessKey, secretKey, bucket string, useSSL bool) (*MinIO, error) {
	store, exists, err := connectBucket(ctx, endpoint, accessKey, secretKey, bucket, useSSL)
	if err != nil {
		return nil, err
	}
	if !exists {
		return nil, fmt.Errorf("bucket %q does not exist", bucket)
	}
	return store, nil
}

func connectBucket(ctx context.Context, endpoint, accessKey, secretKey, bucket string, useSSL bool) (*MinIO, bool, error) {
	client, err := minio.New(endpoint, &minio.Options{
		Creds:  credentials.NewStaticV4(accessKey, secretKey, ""),
		Secure: useSSL,
	})
	if err != nil {
		return nil, false, err
	}
	exists, err := client.BucketExists(ctx, bucket)
	if err != nil {
		return nil, false, err
	}
	return &MinIO{client: client, bucket: bucket}, exists, nil
}

func (m *MinIO) Put(ctx context.Context, objectName string, reader io.Reader, size int64, contentType string) error {
	_, err := m.client.PutObject(ctx, m.bucket, objectName, reader, size, minio.PutObjectOptions{ContentType: contentType})
	return err
}

func (m *MinIO) URI(prefix string) string {
	return fmt.Sprintf("s3://%s/%s", m.bucket, prefix)
}

// OpenObject leaves read limits and integrity verification to the caller.
func (m *MinIO) OpenObject(ctx context.Context, objectName string) (io.ReadCloser, error) {
	return m.client.GetObject(ctx, m.bucket, objectName, minio.GetObjectOptions{})
}

// DownloadPrefix materializes one model snapshot prefix into a local directory.
// Object names are treated as relative paths and path traversal is rejected.
// The caller owns cleanup of destDir after the one-shot Job finishes.
func (m *MinIO) DownloadPrefix(ctx context.Context, prefix, destDir string) (int, int64, error) {
	prefix = strings.TrimLeft(strings.TrimSpace(prefix), "/")
	if prefix == "" {
		return 0, 0, fmt.Errorf("object prefix is required")
	}
	if !strings.HasSuffix(prefix, "/") {
		prefix += "/"
	}
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return 0, 0, err
	}

	var files int
	var total int64
	for object := range m.client.ListObjects(ctx, m.bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
		if object.Err != nil {
			return files, total, object.Err
		}
		rel := strings.TrimPrefix(object.Key, prefix)
		if rel == "" || strings.HasSuffix(rel, "/") {
			continue
		}
		clean := filepath.Clean(filepath.FromSlash(rel))
		if clean == "." || clean == ".." || filepath.IsAbs(clean) || strings.HasPrefix(clean, ".."+string(os.PathSeparator)) {
			return files, total, fmt.Errorf("unsafe object path %q", object.Key)
		}
		target := filepath.Join(destDir, clean)
		if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
			return files, total, err
		}
		if err := m.client.FGetObject(ctx, m.bucket, object.Key, target, minio.GetObjectOptions{}); err != nil {
			return files, total, fmt.Errorf("download %s: %w", object.Key, err)
		}
		files++
		total += object.Size
	}
	if files == 0 {
		return 0, 0, fmt.Errorf("no objects found under prefix %q", prefix)
	}
	return files, total, nil
}

// ParseS3URI converts s3://bucket/prefix/ into bucket + object prefix.
func ParseS3URI(raw string) (string, string, error) {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil {
		return "", "", err
	}
	if u.Scheme != "s3" || strings.TrimSpace(u.Host) == "" {
		return "", "", fmt.Errorf("invalid s3 artifact URI %q", raw)
	}
	prefix := strings.TrimLeft(u.Path, "/")
	if prefix == "" {
		return "", "", fmt.Errorf("artifact URI has empty prefix: %q", raw)
	}
	return u.Host, prefix, nil
}

// DownloadObject materializes one exact object into destPath.
func (m *MinIO) DownloadObject(ctx context.Context, objectName, destPath string) (int64, error) {
	objectName = strings.TrimLeft(strings.TrimSpace(objectName), "/")
	if objectName == "" {
		return 0, fmt.Errorf("object name is required")
	}
	if err := os.MkdirAll(filepath.Dir(destPath), 0o755); err != nil {
		return 0, err
	}
	info, err := m.client.StatObject(ctx, m.bucket, objectName, minio.StatObjectOptions{})
	if err != nil {
		return 0, fmt.Errorf("stat %s: %w", objectName, err)
	}
	if err := m.client.FGetObject(ctx, m.bucket, objectName, destPath, minio.GetObjectOptions{}); err != nil {
		return 0, fmt.Errorf("download %s: %w", objectName, err)
	}
	return info.Size, nil
}
