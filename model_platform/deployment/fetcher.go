package main

import (
	"context"

	serving "model-platform/deployment/runtime"
)

// Keep MinIO connection lazy for preparation, but retain the local cache purge
// capability when this production object is passed through serving.Fetcher.
// Purging a stopped deployment must not depend on MinIO availability.
type servingFetcher struct {
	root    string
	connect func(context.Context) (serving.ArtifactStore, error)
}

func newServingFetcher(root string, connect func(context.Context) (serving.ArtifactStore, error)) servingFetcher {
	return servingFetcher{root: root, connect: connect}
}

func (f servingFetcher) Prepare(ctx context.Context, p serving.Plan) error {
	store, err := f.connect(ctx)
	if err != nil {
		return err
	}
	return (serving.SnapshotFetcher{Root: f.root, Store: store}).Prepare(ctx, p)
}

func (f servingFetcher) Purge(ctx context.Context, id string) error {
	return (serving.SnapshotFetcher{Root: f.root}).Purge(ctx, id)
}

var _ serving.Fetcher = servingFetcher{}
var _ interface {
	Purge(context.Context, string) error
} = servingFetcher{}
