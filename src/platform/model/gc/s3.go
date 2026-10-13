package main

import (
	"context"
	"fmt"
	"io"
	"os"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

type s3Store struct {
	client *minio.Client
	bucket string
}

func connectStore(ctx context.Context, useSSL bool) (*s3Store, error) {
	access, secret := os.Getenv("MINIO_ACCESS_KEY"), os.Getenv("MINIO_SECRET_KEY")
	if access == "" || secret == "" {
		return nil, fmt.Errorf("MinIO credentials are required outside dry-run mode")
	}
	bucket := env("GC_LOG_BUCKET", "model-job-logs")
	if bucket == env("MINIO_BUCKET", "model-artifacts") {
		return nil, fmt.Errorf("GC_LOG_BUCKET must be separate from the model artifact bucket")
	}
	client, err := minio.New(env("MINIO_ENDPOINT", "ops-minio1:9000"), &minio.Options{
		Creds: credentials.NewStaticV4(access, secret, ""), Secure: useSSL,
	})
	if err != nil {
		return nil, err
	}
	exists, err := client.BucketExists(ctx, bucket)
	if err != nil {
		return nil, err
	}
	if !exists {
		if err := client.MakeBucket(ctx, bucket, minio.MakeBucketOptions{}); err != nil {
			// Another collector may have just created the same bucket.
			if ok, checkErr := client.BucketExists(ctx, bucket); checkErr != nil || !ok {
				return nil, err
			}
		}
	}
	return &s3Store{client: client, bucket: bucket}, nil
}

func (s *s3Store) Get(ctx context.Context, key string) (io.ReadCloser, error) {
	obj, err := s.client.GetObject(ctx, s.bucket, key, minio.GetObjectOptions{})
	if err != nil {
		return nil, err
	}
	// GetObject is lazy: Stat forces missing/access errors before handing the
	// reader to the immutable upload protocol.
	if _, err := obj.Stat(); err != nil {
		obj.Close()
		code := minio.ToErrorResponse(err).Code
		if code == "NoSuchKey" || code == "NoSuchObject" || code == "NotFound" {
			return nil, os.ErrNotExist
		}
		return nil, err
	}
	return obj, nil
}

func (s *s3Store) Put(ctx context.Context, key string, r io.Reader, size int64) error {
	_, err := s.client.PutObject(ctx, s.bucket, key, r, size, minio.PutObjectOptions{ContentType: "application/octet-stream"})
	return err
}

func (s *s3Store) URI(key string) string { return "s3://" + s.bucket + "/" + key }
