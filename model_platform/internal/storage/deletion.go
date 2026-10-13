package storage

import (
	"context"
	"fmt"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"strings"
)

// PurgeModel can only target a complete UUID-scoped model namespace, never a
// caller-supplied bucket or arbitrary prefix. Versions and delete markers count.
func (m *MinIO) PurgeModel(ctx context.Context, id string) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id {
		return fmt.Errorf("invalid model purge identity")
	}
	opts := minio.ListObjectsOptions{Prefix: "models/" + id + "/", Recursive: true, WithVersions: true}
	for item := range m.client.ListObjects(ctx, m.bucket, opts) {
		if !strings.HasPrefix(item.Key, opts.Prefix) && item.Err == nil {
			return fmt.Errorf("storage returned an object outside selected model")
		}
		if item.Err != nil {
			return item.Err
		}
		if e = m.client.RemoveObject(ctx, m.bucket, item.Key, minio.RemoveObjectOptions{VersionID: item.VersionID}); e != nil {
			return e
		}
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	for item := range m.client.ListObjects(ctx, m.bucket, opts) {
		if item.Err != nil {
			return item.Err
		}
		return fmt.Errorf("model object versions remain")
	}
	return ctx.Err()
}
