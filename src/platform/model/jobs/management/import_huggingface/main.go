package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime"
	"model-platform/internal/operationreport"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	"model-platform/internal/storage"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
)

type hfModelInfo struct {
	SHA      string `json:"sha"`
	Siblings []struct {
		Filename string `json:"rfilename"`
	} `json:"siblings"`
}
type manifestFile struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size_bytes"`
}
type manifest struct {
	RepoID            string         `json:"repo_id"`
	RequestedRevision string         `json:"requested_revision"`
	ResolvedRevision  string         `json:"resolved_revision"`
	Files             []manifestFile `json:"files"`
	FileSelection     string         `json:"file_selection,omitempty"`
	ExcludedFiles     []string       `json:"excluded_files,omitempty"`
}

func main() {
	// [FLOW-14] Docker one-shot Job entrypoint. The Dispatcher only gives this
	// process generic execution identifiers through environment variables.
	ctx := context.Background()
	operationID := required("OPERATION_ID")
	modelVersionID := required("RESOURCE_ID")
	opConn, err := grpc.NewClient(env("OPERATION_GRPC_ADDRESS", "operation:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		log.Fatal(err)
	}
	defer opConn.Close()
	ops := operationreport.New(operationv1.NewOperationServiceClient(opConn))
	if err := ops.Authorize(ctx, operationID, modelVersionID, "management.import-huggingface"); err != nil {
		log.Printf("execution not authorized; process stopped: %v", err)
		return
	}
	mgmtConn, err := grpc.NewClient(env("MANAGEMENT_GRPC_ADDRESS", "management:9000"), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		fail(ctx, ops, nil, operationID, modelVersionID, "GRPC_CONNECT_FAILED", err.Error(), true)
		return
	}
	defer mgmtConn.Close()
	management := managementv1.NewManagementServiceClient(mgmtConn)
	if _, err := ops.MarkRunning(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "resolving_source"}); err != nil {
		log.Printf("mark running: %v", err)
		return
	}
	// [FLOW-15] The Job resolves domain data and the short-lived credential by
	// resource_id. The HF token never passed through Kafka or docker run -e.
	spec, err := management.GetImportSpec(ctx, &managementv1.GetImportSpecRequest{ModelVersionId: modelVersionID})
	if err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "IMPORT_SPEC_FAILED", err.Error(), false)
		return
	}
	credential, err := management.GetImportCredential(ctx, &managementv1.GetImportCredentialRequest{ModelVersionId: modelVersionID})
	if err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "IMPORT_CREDENTIAL_FAILED", err.Error(), false)
		return
	}
	hfToken := credential.GetHfToken()

	// [FLOW-16] Resolve the requested revision to an immutable HF commit SHA.
	info, err := resolveHF(ctx, spec.GetRepoId(), spec.GetRequestedRevision(), hfToken)
	if err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "HUGGINGFACE_RESOLVE_FAILED", err.Error(), true)
		return
	}
	store, useSSLErr := connectStore(ctx)
	if useSSLErr != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "MINIO_CONNECT_FAILED", useSSLErr.Error(), true)
		return
	}
	prefix := fmt.Sprintf("models/%s/versions/%s/", spec.GetModelId(), modelVersionID)
	if _, err := ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "transferring_files"}); err != nil {
		log.Printf("update phase: %v", err)
	}
	// [FLOW-17] Transfer one file at a time: HF -> temp -> SHA256 -> MinIO -> delete temp.
	selected, selection, excluded := selectSnapshotFiles(spec.GetRepoId(), info.SHA, info.Siblings)
	if selection != "" {
		log.Printf("snapshot_selection policy=%s selected=%d excluded=%d", selection, len(selected), len(excluded))
	}
	files, total, err := transferFiles(ctx, store, spec.GetRepoId(), info.SHA, selected, prefix, hfToken, progressReport(ctx, ops, operationID))
	if err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "TRANSFER_FAILED", err.Error(), true)
		return
	}
	if _, err := ops.UpdatePhase(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "writing_manifest"}); err != nil {
		log.Printf("manifest phase report unavailable")
	}
	manifestPayload, err := json.MarshalIndent(manifest{RepoID: spec.GetRepoId(), RequestedRevision: spec.GetRequestedRevision(), ResolvedRevision: info.SHA, Files: files, FileSelection: selection, ExcludedFiles: excluded}, "", "  ")
	if err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "MANIFEST_FAILED", err.Error(), false)
		return
	}
	manifestHash := sha256.Sum256(manifestPayload)
	if err := store.Put(ctx, prefix+"_manifest.json", strings.NewReader(string(manifestPayload)), int64(len(manifestPayload)), "application/json"); err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "MANIFEST_UPLOAD_FAILED", err.Error(), true)
		return
	}
	// [FLOW-18] Domain completion also deletes the encrypted temporary credential.
	if _, err := management.CompleteImport(ctx, &managementv1.CompleteImportRequest{ModelVersionId: modelVersionID, ResolvedRevision: info.SHA, ArtifactUri: store.URI(prefix), ArtifactSha256: hex.EncodeToString(manifestHash[:]), ArtifactSizeBytes: total}); err != nil {
		fail(ctx, ops, management, operationID, modelVersionID, "MANAGEMENT_COMPLETE_FAILED", err.Error(), true)
		return
	}
	// [FLOW-19] Operation is the common source of execution lifecycle truth.
	if _, err := ops.SucceedOperation(ctx, &operationv1.UpdateOperationRequest{OperationId: operationID, Phase: "completed"}); err != nil {
		log.Printf("domain completed but operation completion report failed: %v", err)
		return
	}
	log.Printf("import completed operation=%s model_version=%s files=%d bytes=%d", operationID, modelVersionID, len(files), total)
}

func resolveHF(ctx context.Context, repoID, revision, token string) (hfModelInfo, error) {
	endpoint := "https://huggingface.co/api/models/" + escapePath(repoID) + "/revision/" + url.PathEscape(revision)
	res, err := getHF(ctx, endpoint, token)
	if err != nil {
		return hfModelInfo{}, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return hfModelInfo{}, fmt.Errorf("huggingface model info status=%d", res.StatusCode)
	}
	var info hfModelInfo
	if err := json.NewDecoder(res.Body).Decode(&info); err != nil {
		return hfModelInfo{}, err
	}
	if info.SHA == "" || len(info.Siblings) == 0 {
		return hfModelInfo{}, fmt.Errorf("huggingface returned no snapshot files")
	}
	return info, nil
}

type transferStore interface {
	Put(context.Context, string, io.Reader, int64, string) error
}

func transferFiles(ctx context.Context, store transferStore, repoID, resolved string, siblings []struct {
	Filename string `json:"rfilename"`
}, prefix, token string, report func(*operationv1.TransferProgress)) ([]manifestFile, int64, error) {
	files := make([]manifestFile, 0, len(siblings))
	var total int64
	meter := &transferMeter{report: report}
	for _, sibling := range siblings {
		if strings.TrimSpace(sibling.Filename) != "" {
			meter.sample.FilesTotal++
		}
	}
	for _, sibling := range siblings {
		rel := strings.TrimSpace(sibling.Filename)
		if rel == "" {
			continue
		}
		meter.sample.CurrentFile = rel
		meter.sample.Stage = "downloading"
		meter.sample.BytesCompleted = 0
		meter.sample.BytesTotal = 0
		meter.emit(true)
		tmp, hash, size, err := downloadOne(ctx, repoID, resolved, rel, token, meter)
		if err != nil {
			return nil, 0, fmt.Errorf("download %s: %w", rel, err)
		}
		contentType := mime.TypeByExtension(filepath.Ext(rel))
		if contentType == "" {
			contentType = "application/octet-stream"
		}
		file, err := os.Open(tmp)
		if err != nil {
			_ = os.Remove(tmp)
			return nil, 0, err
		}
		meter.sample.Stage = "uploading"
		meter.emit(true)
		uploadErr := store.Put(ctx, prefix+rel, file, size, contentType)
		closeErr := file.Close()
		removeErr := os.Remove(tmp)
		if uploadErr != nil {
			return nil, 0, fmt.Errorf("upload %s: %w", rel, uploadErr)
		}
		if closeErr != nil {
			return nil, 0, closeErr
		}
		if removeErr != nil {
			log.Printf("remove temp %s: %v", tmp, removeErr)
		}
		meter.sample.FilesCompleted++
		meter.sample.Stage = "stored"
		meter.emit(true)
		files = append(files, manifestFile{Path: rel, SHA256: hash, Size: size})
		total += size
		log.Printf("transferred %s (%d bytes)", rel, size)
	}
	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	if len(files) == 0 {
		return nil, 0, fmt.Errorf("no files transferred")
	}
	return files, total, nil
}

func downloadOne(ctx context.Context, repoID, resolved, rel, token string, meter *transferMeter) (string, string, int64, error) {
	endpoint := "https://huggingface.co/" + escapePath(repoID) + "/resolve/" + url.PathEscape(resolved) + "/" + escapePath(rel) + "?download=true"
	res, err := getHF(ctx, endpoint, token)
	if err != nil {
		return "", "", 0, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return "", "", 0, fmt.Errorf("status %d", res.StatusCode)
	}
	tmp, err := os.CreateTemp("", "model-import-*")
	if err != nil {
		return "", "", 0, err
	}
	path := tmp.Name()
	hash := sha256.New()
	if meter != nil {
		meter.sample.BytesTotal = res.ContentLength
		if meter.sample.BytesTotal < 0 {
			meter.sample.BytesTotal = 0
		}
		meter.emit(true)
	}
	written, copyErr := io.Copy(io.MultiWriter(tmp, hash, meter), res.Body)
	closeErr := tmp.Close()
	if copyErr != nil {
		_ = os.Remove(path)
		return "", "", 0, copyErr
	}
	if closeErr != nil {
		_ = os.Remove(path)
		return "", "", 0, closeErr
	}
	if meter != nil {
		meter.sample.BytesCompleted = written
		meter.sample.BytesTotal = written
		meter.emit(true)
	}
	return path, hex.EncodeToString(hash.Sum(nil)), written, nil
}

func connectStore(ctx context.Context) (*storage.MinIO, error) {
	useSSL, err := strconv.ParseBool(env("MINIO_USE_SSL", "false"))
	if err != nil {
		return nil, err
	}
	return storage.Connect(ctx, required("MINIO_ENDPOINT"), required("MINIO_ACCESS_KEY"), required("MINIO_SECRET_KEY"), env("MINIO_BUCKET", "model-artifacts"), useSSL)
}
func fail(ctx context.Context, ops operationv1.OperationServiceClient, management managementv1.ManagementServiceClient, operationID, modelVersionID, code, message string, retryable bool) {
	if management != nil {
		_, _ = management.FailImport(ctx, &managementv1.FailImportRequest{ModelVersionId: modelVersionID})
	}
	if ops != nil {
		_, _ = ops.FailOperation(ctx, &operationv1.FailOperationRequest{OperationId: operationID, Phase: "failed", ErrorCode: code, ErrorMessage: message, Retryable: retryable})
	}
	log.Printf("import failed code=%s message=%s", code, message)
}
func setHFHeaders(req *http.Request, token string) {
	req.Header.Set("User-Agent", "model-platform-import-job/1.0")
	if strings.TrimSpace(token) != "" {
		req.Header.Set("Authorization", "Bearer "+strings.TrimSpace(token))
	}
}
func escapePath(value string) string {
	parts := strings.Split(value, "/")
	for i := range parts {
		parts[i] = url.PathEscape(parts[i])
	}
	return strings.Join(parts, "/")
}
func required(name string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	log.Fatalf("%s is required", name)
	return ""
}
func env(name, fallback string) string {
	if v := os.Getenv(name); v != "" {
		return v
	}
	return fallback
}
