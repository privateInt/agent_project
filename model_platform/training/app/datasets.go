package app

import (
	"context"
	"errors"
	"io"
	"os"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/datasets"
	"model-platform/internal/idempotency"
    "model-platform/internal/listing"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const datasetSchema = `
CREATE TABLE IF NOT EXISTS datasets (
 dataset_id UUID PRIMARY KEY,
 name TEXT NOT NULL CHECK(length(name) BETWEEN 1 AND 128),
 description TEXT NOT NULL DEFAULT '' CHECK(length(description)<=4096),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS dataset_versions (
 dataset_version_id UUID PRIMARY KEY,
 dataset_id UUID NOT NULL REFERENCES datasets(dataset_id),
 version VARCHAR(64) NOT NULL CHECK(version ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$'),
 state VARCHAR(16) NOT NULL DEFAULT 'ready' CHECK(state='ready'),
 format VARCHAR(32) NOT NULL DEFAULT 'chat_messages' CHECK(format='chat_messages'),
 artifact_uri TEXT NOT NULL,
 sha256 VARCHAR(64) NOT NULL CHECK(sha256 ~ '^[0-9a-f]{64}$'),
 size_bytes BIGINT NOT NULL CHECK(size_bytes BETWEEN 1 AND 67108864),
 record_count BIGINT NOT NULL CHECK(record_count>=2),
 created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
 UNIQUE(dataset_id,version)
);
CREATE INDEX IF NOT EXISTS dataset_versions_dataset_idx ON dataset_versions(dataset_id,dataset_version_id);
`

type DatasetStore interface {
	Put(context.Context, string, io.Reader, int64, string) error
	OpenObject(context.Context, string) (io.ReadCloser, error)
	URI(string) string
}

// ConfigureDatasets must be called before serving requests.
func (s *Service) ConfigureDatasets(store DatasetStore) { s.datasetStore = store }

func datasetUUID(value, field string) (string, error) {
	id, err := uuid.Parse(strings.TrimSpace(value))
	if err != nil || id == uuid.Nil { return "", status.Errorf(codes.InvalidArgument, "%s must be a UUID", field) }
	return id.String(), nil
}

func (s *Service) CreateDataset(ctx context.Context, req *trainingv1.CreateDatasetRequest) (*trainingv1.Dataset, error) {
	name, description := strings.TrimSpace(req.GetName()), strings.TrimSpace(req.GetDescription())
	if !utf8.ValidString(name) || !utf8.ValidString(description) || strings.ContainsRune(name, 0) || strings.ContainsRune(description, 0) || utf8.RuneCountInString(name) < 1 || utf8.RuneCountInString(name) > 128 || utf8.RuneCountInString(description) > 4096 {
		return nil, status.Error(codes.InvalidArgument, "name requires 1..128 characters; description allows up to 4096 characters")
	}
	fingerprint, err := idempotency.Fingerprint(struct { Version int; Name, Description string }{1, name, description}, nil)
	if err != nil { return nil, err }
	response := &trainingv1.Dataset{}
	err = idempotency.Execute(ctx, s.pool, "dataset.create.v1", req.GetIdempotencyKey(), fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		var created time.Time
		id := uuid.NewString()
		if err := tx.QueryRow(ctx, `INSERT INTO datasets(dataset_id,name,description) VALUES($1,$2,$3) RETURNING created_at`, id, name, description).Scan(&created); err != nil { return err }
		response.DatasetId, response.Name, response.Description, response.CreatedAt = id, name, description, timestamppb.New(created)
		return nil
	})
	return response, err
}

func scanDataset(row pgx.Row) (*trainingv1.Dataset, error) {
	d := &trainingv1.Dataset{}
	var created time.Time
	err := row.Scan(&d.DatasetId, &d.Name, &d.Description, &created)
	if errors.Is(err, pgx.ErrNoRows) { return nil, status.Error(codes.NotFound, "dataset not found") }
	if err != nil { return nil, idempotency.DBError(err) }
	d.CreatedAt = timestamppb.New(created)
	return d, nil
}

func (s *Service) GetDataset(ctx context.Context, req *trainingv1.GetDatasetRequest) (*trainingv1.Dataset, error) {
	id, err := datasetUUID(req.GetDatasetId(), "dataset_id")
	if err != nil { return nil, err }
	return scanDataset(s.pool.QueryRow(ctx, `SELECT dataset_id,name,description,created_at FROM datasets WHERE dataset_id=$1`, id))
}

func datasetPage(limit int32, after string) (int, string, error) {
	if limit == 0 { limit = 50 }
	if limit < 1 || limit > 100 { return 0, "", status.Error(codes.InvalidArgument, "limit must be 1..100") }
	if after == "" { return int(limit), uuid.Nil.String(), nil }
	id, err := datasetUUID(after, "after_id")
	return int(limit), id, err
}

func (s *Service) ListDatasets(ctx context.Context, req *trainingv1.ListDatasetsRequest) (*trainingv1.ListDatasetsResponse, error) {
	limit, after, err := datasetPage(req.GetLimit(), req.GetAfterId())
	if err != nil { return nil, err }
	text, err := listing.Text(req.GetQ())
    if err != nil { return nil, err }
    rows, err := s.pool.Query(ctx, `SELECT dataset_id,name,description,created_at FROM datasets WHERE dataset_id>$1::uuid AND (name ILIKE $3 ESCAPE '\' OR description ILIKE $3 ESCAPE '\') ORDER BY dataset_id LIMIT $2`, after, limit+1, listing.Pattern(text))
	if err != nil { return nil, idempotency.DBError(err) }
	defer rows.Close()
	out := &trainingv1.ListDatasetsResponse{}
	for rows.Next() {
		if len(out.Datasets) == limit { out.NextAfterId = out.Datasets[limit-1].GetDatasetId(); break }
		d, err := scanDataset(rows)
		if err != nil { return nil, err }
		out.Datasets = append(out.Datasets, d)
	}
	return out, idempotency.DBError(rows.Err())
}

const datasetVersionColumns = `dataset_version_id,dataset_id,version,state,format,artifact_uri,sha256,size_bytes,record_count,created_at`

func scanDatasetVersion(row pgx.Row) (*trainingv1.DatasetVersion, error) {
	v := &trainingv1.DatasetVersion{}
	var created time.Time
	err := row.Scan(&v.DatasetVersionId, &v.DatasetId, &v.Version, &v.State, &v.Format, &v.ArtifactUri, &v.Sha256, &v.SizeBytes, &v.RecordCount, &created)
	if errors.Is(err, pgx.ErrNoRows) { return nil, status.Error(codes.NotFound, "dataset version not found") }
	if err != nil { return nil, idempotency.DBError(err) }
	v.CreatedAt = timestamppb.New(created)
	return v, nil
}

func (s *Service) GetDatasetVersion(ctx context.Context, req *trainingv1.GetDatasetVersionRequest) (*trainingv1.DatasetVersion, error) {
	id, err := datasetUUID(req.GetDatasetVersionId(), "dataset_version_id")
	if err != nil { return nil, err }
	return scanDatasetVersion(s.pool.QueryRow(ctx, `SELECT `+datasetVersionColumns+` FROM dataset_versions WHERE dataset_version_id=$1`, id))
}

func (s *Service) ListDatasetVersions(ctx context.Context, req *trainingv1.ListDatasetVersionsRequest) (*trainingv1.ListDatasetVersionsResponse, error) {
	d, err := s.GetDataset(ctx, &trainingv1.GetDatasetRequest{DatasetId: req.GetDatasetId()})
	if err != nil { return nil, err }
	limit, after, err := datasetPage(req.GetLimit(), req.GetAfterId())
	if err != nil { return nil, err }
	rows, err := s.pool.Query(ctx, `SELECT `+datasetVersionColumns+` FROM dataset_versions WHERE dataset_id=$1 AND dataset_version_id>$2::uuid ORDER BY dataset_version_id LIMIT $3`, d.GetDatasetId(), after, limit+1)
	if err != nil { return nil, idempotency.DBError(err) }
	defer rows.Close()
	out := &trainingv1.ListDatasetVersionsResponse{}
	for rows.Next() {
		if len(out.Versions) == limit { out.NextAfterId = out.Versions[limit-1].GetDatasetVersionId(); break }
		v, err := scanDatasetVersion(rows)
		if err != nil { return nil, err }
		out.Versions = append(out.Versions, v)
	}
	return out, idempotency.DBError(rows.Err())
}

var datasetVersionPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`)

func (s *Service) UploadDatasetVersion(stream grpc.ClientStreamingServer[trainingv1.UploadDatasetVersionRequest, trainingv1.DatasetVersion]) error {
	first, err := stream.Recv()
	if err == io.EOF { return status.Error(codes.InvalidArgument, "upload header is required") }
	if err != nil { return err }
	header := first.GetHeader()
	if header == nil { return status.Error(codes.InvalidArgument, "first upload message must contain a header") }
	id, err := datasetUUID(header.GetDatasetId(), "dataset_id")
	if err != nil { return err }
	version := strings.TrimSpace(header.GetVersion())
	if !datasetVersionPattern.MatchString(version) { return status.Error(codes.InvalidArgument, "version requires 1..64 ASCII letters, digits, '.', '_' or '-', starting with a letter or digit") }
	if err := idempotency.ValidateKey(header.GetIdempotencyKey()); err != nil { return err }
	if _, err := s.GetDataset(stream.Context(), &trainingv1.GetDatasetRequest{DatasetId: id}); err != nil { return err }
	tmp, err := os.CreateTemp("", "dataset-upload-*.jsonl")
	if err != nil { return status.Error(codes.Internal, "create dataset upload workspace") }
	defer os.Remove(tmp.Name())
	defer tmp.Close()
	var size int64
	for {
		message, err := stream.Recv()
		if err == io.EOF { break }
		if err != nil { return err }
		chunk := message.GetChunk()
		if message.GetHeader() != nil || len(chunk) == 0 || len(chunk) > datasets.ChunkBytes { return status.Error(codes.InvalidArgument, "upload expects nonempty chunks of at most 256 KiB after the header") }
		size += int64(len(chunk))
		if size > datasets.MaxBytes { return status.Error(codes.ResourceExhausted, "dataset upload exceeds 64 MiB") }
		if _, err := tmp.Write(chunk); err != nil { return status.Error(codes.Internal, "write dataset upload workspace") }
	}
	if _, err := tmp.Seek(0, io.SeekStart); err != nil { return status.Error(codes.Internal, "read dataset upload workspace") }
	info, err := datasets.Inspect(tmp)
	if err != nil { return status.Errorf(codes.InvalidArgument, "invalid dataset: %v", err) }
	response, err := s.registerDatasetVersion(stream.Context(), id, version, header.GetIdempotencyKey(), tmp, info)
	if err != nil { return err }
	return stream.SendAndClose(response)
}

func matchesDatasetContent(v *trainingv1.DatasetVersion, info datasets.Info) bool {
	return v.GetState() == "ready" && v.GetFormat() == "chat_messages" && v.GetSha256() == info.SHA256 && v.GetSizeBytes() == info.SizeBytes && v.GetRecordCount() == info.RecordCount
}

func (s *Service) registerDatasetVersion(ctx context.Context, id, version, key string, file *os.File, info datasets.Info) (*trainingv1.DatasetVersion, error) {
	fingerprint, err := idempotency.Fingerprint(struct { Version int; DatasetID, Label string; Content datasets.Info }{1, id, version, info}, nil)
	if err != nil { return nil, err }
	previous, err := scanDatasetVersion(s.pool.QueryRow(ctx, `SELECT `+datasetVersionColumns+` FROM dataset_versions WHERE dataset_id=$1 AND version=$2`, id, version))
	if err != nil && status.Code(err) != codes.NotFound { return nil, err }
	uri := ""
	if previous != nil {
		if !matchesDatasetContent(previous, info) { return nil, status.Error(codes.AlreadyExists, "dataset version is immutable; choose a new version label") }
		uri = previous.GetArtifactUri()
	} else {
		if s.datasetStore == nil { return nil, status.Error(codes.Unavailable, "dataset storage is not configured") }
		object := "datasets/"+id+"/sha256/"+info.SHA256+"/train.jsonl"
		if _, err := file.Seek(0, io.SeekStart); err != nil { return nil, status.Error(codes.Internal, "read dataset upload workspace") }
		if err := s.datasetStore.Put(ctx, object, file, info.SizeBytes, "application/x-ndjson"); err != nil { return nil, status.Error(codes.Unavailable, "dataset storage upload failed; retry with the same version and key") }
		remote, err := s.datasetStore.OpenObject(ctx, object)
		if err != nil { return nil, status.Error(codes.Unavailable, "dataset storage readback failed") }
		verifyErr := datasets.Verify(remote, info)
		closeErr := remote.Close()
		if verifyErr != nil || closeErr != nil { return nil, status.Error(codes.Unavailable, "dataset storage integrity verification failed") }
		uri = s.datasetStore.URI(object)
	}
	response := &trainingv1.DatasetVersion{}
	// Object I/O is deliberately outside the short DB transaction. A failed DB
	// commit can leave an unreferenced content object; no ready version is exposed.
	err = idempotency.Execute(ctx, s.pool, "dataset.version.upload.v1", key, fingerprint, response, func(ctx context.Context, tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `INSERT INTO dataset_versions(dataset_version_id,dataset_id,version,artifact_uri,sha256,size_bytes,record_count) VALUES($1,$2,$3,$4,$5,$6,$7) ON CONFLICT(dataset_id,version) DO NOTHING`, uuid.NewString(), id, version, uri, info.SHA256, info.SizeBytes, info.RecordCount)
		if err != nil { return err }
		stored, err := scanDatasetVersion(tx.QueryRow(ctx, `SELECT `+datasetVersionColumns+` FROM dataset_versions WHERE dataset_id=$1 AND version=$2`, id, version))
		if err != nil { return err }
		if !matchesDatasetContent(stored, info) { return status.Error(codes.AlreadyExists, "dataset version is immutable; choose a new version label") }
		proto.Merge(response, stored)
		return nil
	})
	return response, err
}

func pinDataset(ctx context.Context, tx pgx.Tx, requested DatasetSpec) (DatasetSpec, error) {
	if requested.SourceType != "version" { return requested, nil }
	v, err := scanDatasetVersion(tx.QueryRow(ctx, `SELECT `+datasetVersionColumns+` FROM dataset_versions WHERE dataset_version_id=$1`, requested.DatasetVersionID))
	if err != nil { return DatasetSpec{}, err }
	info := datasets.Info{SHA256: v.GetSha256(), SizeBytes: v.GetSizeBytes(), RecordCount: v.GetRecordCount()}
	if v.GetState() != "ready" || v.GetFormat() != requested.Format || info.Validate() != nil { return DatasetSpec{}, status.Error(codes.FailedPrecondition, "dataset version is not ready") }
	requested.SourceURI, requested.SHA256, requested.SizeBytes, requested.RecordCount = v.GetArtifactUri(), info.SHA256, info.SizeBytes, info.RecordCount
	return requested, nil
}
