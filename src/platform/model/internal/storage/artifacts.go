package storage

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/minio/minio-go/v7"
	"model-platform/internal/artifacts"
)

func (m *MinIO) Bucket() string { return m.bucket }
func (m *MinIO) missing(ctx context.Context, key string) (bool, error) {
	_, err := m.client.StatObject(ctx, m.bucket, key, minio.StatObjectOptions{})
	if err == nil {
		return false, nil
	}
	code := minio.ToErrorResponse(err).Code
	if code == "NoSuchKey" || code == "NoSuchObject" || code == "NotFound" {
		return true, nil
	}
	return false, err
}
func (m *MinIO) readObject(ctx context.Context, key string) (io.ReadCloser, error) {
	obj, err := m.client.GetObject(ctx, m.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	return obj, nil
}
func (m *MinIO) verifyObject(ctx context.Context, key string, expected artifacts.File) error {
	obj, err := m.readObject(ctx, key)
	if err != nil {
		return err
	}
	defer obj.Close()
	digest, size, err := artifacts.HashReader(obj)
	if err != nil {
		return err
	}
	if size != expected.Size || digest != expected.SHA256 {
		return fmt.Errorf("remote artifact checksum mismatch: %s", key)
	}
	return nil
}

// UploadManifest publishes only bytes named by the canonical manifest.
// Existing objects must match exactly; the manifest is uploaded last.
func (m *MinIO) UploadManifest(ctx context.Context, root, prefix string, encoded []byte) error {
	manifest, _, err := artifacts.Parse(encoded)
	if err != nil {
		return err
	}
	if prefix == "" || !strings.HasSuffix(prefix, "/") {
		return errors.New("artifact prefix must end with /")
	}
	for _, entry := range manifest.Files {
		if err := ctx.Err(); err != nil {
			return err
		}
		name := filepath.Join(root, filepath.FromSlash(entry.Path))
		digest, size, err := artifacts.HashFile(name)
		if err != nil {
			return err
		}
		if size != entry.Size || digest != entry.SHA256 {
			return fmt.Errorf("local artifact changed: %s", entry.Path)
		}
		key := prefix + entry.Path
		missing, err := m.missing(ctx, key)
		if err != nil {
			return err
		}
		if missing {
			f, err := os.Open(name)
			if err != nil {
				return err
			}
			_, putErr := m.client.PutObject(ctx, m.bucket, key, f, entry.Size, minio.PutObjectOptions{ContentType: "application/octet-stream"})
			closeErr := f.Close()
			if putErr != nil {
				return putErr
			}
			if closeErr != nil {
				return closeErr
			}
		}
		if err := m.verifyObject(ctx, key, entry); err != nil {
			return err
		}
	}
	key := prefix + artifacts.ManifestName
	missing, err := m.missing(ctx, key)
	if err != nil {
		return err
	}
	if !missing {
		obj, err := m.readObject(ctx, key)
		if err != nil {
			return err
		}
		existing, readErr := io.ReadAll(io.LimitReader(obj, 16<<20))
		closeErr := obj.Close()
		if readErr != nil {
			return readErr
		}
		if closeErr != nil {
			return closeErr
		}
		if !bytes.Equal(existing, encoded) {
			return errors.New("immutable artifact manifest already differs")
		}
	} else {
		_, err = m.client.PutObject(ctx, m.bucket, key, bytes.NewReader(encoded), int64(len(encoded)), minio.PutObjectOptions{ContentType: "application/json"})
		if err != nil {
			return err
		}
	}
	return nil
}

// VerifyManifest reads every object back from S3; ETag is not a SHA256 proof.
// It also rejects unlisted objects under the reserved immutable prefix.
func (m *MinIO) VerifyManifest(ctx context.Context, prefix, expectedDigest string) (artifacts.Manifest, error) {
	var empty artifacts.Manifest
	if prefix == "" || !strings.HasSuffix(prefix, "/") || !artifacts.ValidDigest(expectedDigest) {
		return empty, errors.New("invalid artifact prefix or digest")
	}
	obj, err := m.readObject(ctx, prefix+artifacts.ManifestName)
	if err != nil {
		return empty, err
	}
	encoded, readErr := io.ReadAll(io.LimitReader(obj, 16<<20))
	closeErr := obj.Close()
	if readErr != nil {
		return empty, readErr
	}
	if closeErr != nil {
		return empty, closeErr
	}
	manifest, digest, err := artifacts.Parse(encoded)
	if err != nil {
		return empty, err
	}
	if digest != expectedDigest {
		return empty, errors.New("manifest digest mismatch")
	}
	expected := map[string]artifacts.File{}
	for _, entry := range manifest.Files {
		expected[prefix+entry.Path] = entry
	}
	expected[prefix+artifacts.ManifestName] = artifacts.File{Path: artifacts.ManifestName, Size: int64(len(encoded)), SHA256: digest}
	for object := range m.client.ListObjects(ctx, m.bucket, minio.ListObjectsOptions{Prefix: prefix, Recursive: true}) {
		if object.Err != nil {
			return empty, object.Err
		}
		entry, ok := expected[object.Key]
		if !ok {
			return empty, fmt.Errorf("unlisted artifact object: %s", object.Key)
		}
		if object.Size != entry.Size {
			return empty, fmt.Errorf("artifact size mismatch: %s", object.Key)
		}
		delete(expected, object.Key)
	}
	if len(expected) != 0 {
		return empty, fmt.Errorf("artifact prefix is missing %d objects", len(expected))
	}
	for _, entry := range manifest.Files {
		if err := m.verifyObject(ctx, prefix+entry.Path, entry); err != nil {
			return empty, err
		}
	}
	return manifest, nil
}
