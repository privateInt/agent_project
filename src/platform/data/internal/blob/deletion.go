package blob

import (
	"context"
	"fmt"
	"github.com/google/uuid"
	"github.com/minio/minio-go/v7"
	"strings"
)

// DeleteObject removes every version of exactly one platform-owned key. Object
// lock errors remain errors; a delete marker alone never counts as deletion.
func (s *MinIO) DeleteObject(ctx context.Context, key string) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if key == "" || strings.HasPrefix(key, "/") || strings.Contains(key, "..") || !strings.Contains(key, "/sha256/") {
		return fmt.Errorf("invalid deletion key")
	}
	opts := minio.ListObjectsOptions{Prefix: key, Recursive: true, WithVersions: true}
	for item := range s.client.ListObjects(ctx, s.bucket, opts) {
		if item.Err != nil {
			return item.Err
		}
		if item.Key != key {
			continue
		}
		if e := s.client.RemoveObject(ctx, s.bucket, key, minio.RemoveObjectOptions{VersionID: item.VersionID}); e != nil {
			return e
		}
	}
	if e := ctx.Err(); e != nil {
		return e
	}
	for item := range s.client.ListObjects(ctx, s.bucket, opts) {
		if item.Err != nil {
			return item.Err
		}
		if item.Key == key {
			return fmt.Errorf("object versions remain")
		}
	}
	return ctx.Err()
}

// PurgeRun also removes an output written before a failed completion callback.
func (s *MinIO) PurgeRun(ctx context.Context, id string) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	u, e := uuid.Parse(id)
	if e != nil || u == uuid.Nil || u.String() != id || s.bucket != "data-canonical" {
		return fmt.Errorf("invalid canonical purge identity")
	}
	opts := minio.ListObjectsOptions{Prefix: "canonical/" + id + "/", Recursive: true, WithVersions: true}
	for item := range s.client.ListObjects(ctx, s.bucket, opts) {
		if item.Err != nil {
			return item.Err
		}
		if !strings.HasPrefix(item.Key, opts.Prefix) {
			return fmt.Errorf("object outside selected run")
		}
		if e = s.client.RemoveObject(ctx, s.bucket, item.Key, minio.RemoveObjectOptions{VersionID: item.VersionID}); e != nil {
			return e
		}
	}
	if e = ctx.Err(); e != nil {
		return e
	}
	for item := range s.client.ListObjects(ctx, s.bucket, opts) {
		if item.Err != nil {
			return item.Err
		}
		return fmt.Errorf("canonical object versions remain")
	}
	return ctx.Err()
}
