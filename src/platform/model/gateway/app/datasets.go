package app

import (
	"context"
	"errors"
	"io"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/datasets"

	"github.com/gofiber/fiber/v3"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func datasetRecordMap(d *trainingv1.Dataset) fiber.Map {
	return fiber.Map{"dataset_id": d.GetDatasetId(), "name": d.GetName(), "description": d.GetDescription(), "created_at": d.GetCreatedAt().AsTime().UTC().Format(time.RFC3339Nano)}
}

func datasetVersionMap(v *trainingv1.DatasetVersion) fiber.Map {
	return fiber.Map{
		"dataset_version_id": v.GetDatasetVersionId(), "dataset_id": v.GetDatasetId(), "version": v.GetVersion(),
		"state": v.GetState(), "format": v.GetFormat(), "source_type": "upload", "artifact_uri": v.GetArtifactUri(),
		"sha256": v.GetSha256(), "size_bytes": v.GetSizeBytes(), "record_count": v.GetRecordCount(),
		"created_at": v.GetCreatedAt().AsTime().UTC().Format(time.RFC3339Nano),
	}
}

func (a *API) createDataset(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil { return grpcError(c, err) }
	var body struct { Name string `json:"name"`; Description string `json:"description"` }
	if err := c.Bind().Body(&body); err != nil { return c.Status(400).JSON(fiber.Map{"error": "invalid body"}) }
	ctx, cancel := context.WithTimeout(c.Context(), 25*time.Second)
	defer cancel()
	d, err := a.clients.Training.CreateDataset(ctx, &trainingv1.CreateDatasetRequest{Name: body.Name, Description: body.Description, IdempotencyKey: key})
	if err != nil { return grpcError(c, err) }
	return c.Status(201).JSON(datasetRecordMap(d))
}

func (a *API) getDataset(c fiber.Ctx) error {
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	d, err := a.clients.Training.GetDataset(ctx, &trainingv1.GetDatasetRequest{DatasetId: c.Params("id")})
	if err != nil { return grpcError(c, err) }
	return c.JSON(datasetRecordMap(d))
}

func datasetLimit(c fiber.Ctx) (int32, error) {
	value, err := strconv.ParseInt(c.Query("limit", "50"), 10, 32)
	if err != nil || value < 1 || value > 100 { return 0, status.Error(codes.InvalidArgument, "limit must be 1..100") }
	return int32(value), nil
}

func (a *API) listDatasets(c fiber.Ctx) error {
	limit, err := datasetLimit(c)
	if err != nil { return grpcError(c, err) }
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	res, err := a.clients.Training.ListDatasets(ctx, &trainingv1.ListDatasetsRequest{Limit: limit, AfterId: c.Query("after_id"), Q: c.Query("q")})
	if err != nil { return grpcError(c, err) }
	items := make([]fiber.Map, 0, len(res.GetDatasets()))
	for _, d := range res.GetDatasets() { items = append(items, datasetRecordMap(d)) }
	return c.JSON(fiber.Map{"datasets": items, "next_after_id": res.GetNextAfterId()})
}

func (a *API) listDatasetVersions(c fiber.Ctx) error {
	limit, err := datasetLimit(c)
	if err != nil { return grpcError(c, err) }
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	res, err := a.clients.Training.ListDatasetVersions(ctx, &trainingv1.ListDatasetVersionsRequest{DatasetId: c.Params("id"), Limit: limit, AfterId: c.Query("after_id")})
	if err != nil { return grpcError(c, err) }
	items := make([]fiber.Map, 0, len(res.GetVersions()))
	for _, v := range res.GetVersions() { items = append(items, datasetVersionMap(v)) }
	return c.JSON(fiber.Map{"versions": items, "next_after_id": res.GetNextAfterId()})
}

func (a *API) getDatasetVersion(c fiber.Ctx) error {
	ctx, cancel := context.WithTimeout(c.Context(), 15*time.Second)
	defer cancel()
	v, err := a.clients.Training.GetDatasetVersion(ctx, &trainingv1.GetDatasetVersionRequest{DatasetVersionId: c.Params("id")})
	if err != nil { return grpcError(c, err) }
	return c.JSON(datasetVersionMap(v))
}

func (a *API) uploadDatasetVersion(c fiber.Ctx) error {
	key, err := requestKey(c)
	if err != nil { return grpcError(c, err) }
	form, err := c.MultipartForm()
	if err != nil || form == nil { return c.Status(400).JSON(fiber.Map{"error": "multipart form with version and file is required"}) }
	if len(form.File) != 1 || len(form.File["file"]) != 1 || len(form.Value) != 1 || len(form.Value["version"]) != 1 {
		return c.Status(400).JSON(fiber.Map{"error": "provide exactly one version field and one file field"})
	}
	header := form.File["file"][0]
	if strings.ToLower(filepath.Ext(header.Filename)) != ".jsonl" { return c.Status(400).JSON(fiber.Map{"error": "file must have a .jsonl extension"}) }
	if header.Size > datasets.MaxBytes { return c.Status(413).JSON(fiber.Map{"error": "dataset upload exceeds 64 MiB"}) }
	file, err := header.Open()
	if err != nil { return c.Status(500).JSON(fiber.Map{"error": "open uploaded file"}) }
	defer file.Close()
	ctx, cancel := context.WithTimeout(c.Context(), 2*time.Minute)
	defer cancel()
	stream, err := a.clients.Training.UploadDatasetVersion(ctx)
	if err != nil { return grpcError(c, err) }
	streamError := func(err error) error {
		// A server rejection can arrive as EOF from Send. Receive its actual status.
		if errors.Is(err, io.EOF) { _, err = stream.CloseAndRecv() }
		if err == nil { err = status.Error(codes.Internal, "upload ended before all data was sent") }
		if status.Code(err) == codes.ResourceExhausted { return c.Status(413).JSON(fiber.Map{"error": status.Convert(err).Message()}) }
		return grpcError(c, err)
	}
	err = stream.Send(&trainingv1.UploadDatasetVersionRequest{Payload: &trainingv1.UploadDatasetVersionRequest_Header{Header: &trainingv1.DatasetUploadHeader{DatasetId: c.Params("id"), Version: form.Value["version"][0], IdempotencyKey: key}}})
	if err != nil { return streamError(err) }
	buffer := make([]byte, datasets.ChunkBytes)
	var total int64
	for {
		n, readErr := file.Read(buffer)
		if n > 0 {
			total += int64(n)
			if total > datasets.MaxBytes { return c.Status(413).JSON(fiber.Map{"error": "dataset upload exceeds 64 MiB"}) }
			// Send may retain the protobuf message; each chunk owns its bytes.
			err := stream.Send(&trainingv1.UploadDatasetVersionRequest{Payload: &trainingv1.UploadDatasetVersionRequest_Chunk{Chunk: append([]byte(nil), buffer[:n]...)}})
			if err != nil { return streamError(err) }
		}
		if readErr == io.EOF { break }
		if readErr != nil { return c.Status(500).JSON(fiber.Map{"error": "read uploaded file"}) }
	}
	result, err := stream.CloseAndRecv()
	if err != nil { return streamError(err) }
	return c.Status(201).JSON(datasetVersionMap(result))
}
