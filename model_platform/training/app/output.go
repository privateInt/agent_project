package app

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	managementv1 "model-platform/gen/management/v1"
	operationv1 "model-platform/gen/operation/v1"
	trainingv1 "model-platform/gen/training/v1"
	"model-platform/internal/artifacts"
	"model-platform/internal/idempotency"
	"model-platform/internal/purge"
	"model-platform/internal/trainingcheckpoint"
	"model-platform/internal/trainingoutput"
	"model-platform/internal/trainingparallel"
)

const outputSchema = `
CREATE TABLE IF NOT EXISTS training_output_recoveries (
 recovery_id UUID PRIMARY KEY,
 training_run_id UUID NOT NULL REFERENCES training_runs(training_run_id),
 idempotency_key TEXT NOT NULL,
 manifest_sha256 TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed')),
 error_code TEXT NOT NULL DEFAULT '',
 output_model_version_id TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '',
 requested_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
 UNIQUE(training_run_id,idempotency_key)
);
CREATE UNIQUE INDEX IF NOT EXISTS training_output_active_idx ON training_output_recoveries(training_run_id) WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS training_output_latest_idx ON training_output_recoveries(training_run_id,requested_at DESC,recovery_id DESC);
`

type OutputControl interface {
	OutputStopped(context.Context, *operationv1.Operation) error
}

func (s *Service) ConfigureOutputs(root, node string, store func(context.Context) (trainingoutput.Store, error), control OutputControl) {
	s.outputRoot = root
	s.outputNode = strings.TrimSpace(node)
	s.outputStore = store
	s.outputControl = control
}

func (s *Service) outputIdentity(ctx context.Context, id string) (*trainingv1.TrainingRun, *operationv1.Operation, string, error) {
	run, e := s.get(ctx, id)
	if e != nil {
		return nil, nil, "", e
	}
	if s.outputRoot == "" {
		return run, nil, "", status.Error(codes.Unavailable, "saved output controller unavailable")
	}
	if !localTraining(run.GetResources().GetNodeName()) {
		return run, nil, "", status.Error(codes.FailedPrecondition, "remote output recovery is not supported")
	}
	if run.GetOperationId() == "" {
		return run, nil, "", nil
	}
	op, e := s.clients.Operation.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: run.GetOperationId()})
	if e != nil {
		return run, nil, "", status.Error(codes.Unavailable, "training operation lookup unavailable")
	}
	if !matchesTraining(op, id, run.GetOperationId()) {
		return run, nil, "", status.Error(codes.FailedPrecondition, "training execution identity mismatch")
	}
	if op.GetDispatchToken() == "" {
		return run, op, "", nil
	}
	dir, e := trainingoutput.Directory(s.outputRoot, id, op.GetDispatchToken())
	if e != nil {
		return run, op, "", status.Error(codes.FailedPrecondition, "saved output path invalid")
	}
	return run, op, dir, nil
}

func validateSavedOutput(r trainingoutput.Receipt, run *trainingv1.TrainingRun, op *operationv1.Operation, node string) error {
	if _, e := r.Validate(); e != nil {
		return e
	}
	if !proto.Equal(run.GetResources(), op.GetResources()) {
		return errors.New("saved execution resources mismatch")
	}
	if r.RunID != run.GetTrainingRunId() || r.OperationID != op.GetOperationId() || r.DispatchToken != op.GetDispatchToken() || r.InstanceID != op.GetExecutionInstanceId() || r.Node != node || r.SourceID != run.GetSourceModelVersionId() || op.GetExecutionStartedAt() == nil {
		return errors.New("saved output execution mismatch")
	}
	var result struct {
		DatasetVersionID  string                      `json:"dataset_version_id"`
		DatasetSHA256     string                      `json:"dataset_sha256"`
		DatasetSourceType string                      `json:"dataset_source_type"`
		DatasetURI        string                      `json:"dataset_source_uri"`
		DatasetFormat     string                      `json:"dataset_format"`
		DatasetBytes      int64                       `json:"dataset_bytes"`
		DatasetRecords    int64                       `json:"dataset_record_count"`
		Checkpoint        trainingcheckpoint.Evidence `json:"checkpoint"`
		TrackingID        string                      `json:"mlflow_run_id"`
		Parallelism       trainingparallel.Result     `json:"parallelism"`
	}
	if e := json.Unmarshal(r.Result, &result); e != nil {
		return e
	}
	if e := validateEvaluationResult(string(r.Result), run.GetTrainingRunId(), run.GetSourceModelVersionId(), run.GetTraining(), run.GetEvaluation()); e != nil {
		return e
	}
	ds := run.GetDataset()
	if result.DatasetSourceType != ds.GetSourceType() || result.DatasetURI != ds.GetSourceUri() || result.DatasetFormat != ds.GetFormat() {
		return errors.New("saved dataset source mismatch")
	}
	if ds.GetSourceType() == "version" && (result.DatasetVersionID != ds.GetDatasetVersionId() || result.DatasetSHA256 != ds.GetSha256() || result.DatasetBytes != ds.GetSizeBytes() || result.DatasetRecords != ds.GetRecordCount()) {
		return errors.New("saved dataset pin mismatch")
	}
	if result.TrackingID != run.GetMlflowRunId() {
		return errors.New("saved tracking identity mismatch")
	}
	count := int(run.GetResources().GetGpuCount())
	spec := run.GetTraining()
	if spec.GetCheckpointSteps() > 0 {
		if e := trainingcheckpoint.ValidateEvidence(result.Checkpoint, r.RunID, spec.GetMaxSteps(), trainingcheckpoint.FromProto(run.GetResume())); e != nil {
			return e
		}
	}
	if count > 1 {
		return trainingparallel.Validate(result.Parallelism, r.RunID, count, int64(spec.GetMaxSteps()), int64(spec.GetMicroBatchSize())*int64(spec.GetGradientAccumulationSteps())*int64(count))
	}
	return nil
}

func (s *Service) requireStoppedOutput(ctx context.Context, run *trainingv1.TrainingRun, op *operationv1.Operation) error {
	if (run.GetStatus() != "failed" && run.GetStatus() != "succeeded") || (op.GetStatus() != "failed" && op.GetStatus() != "succeeded") || run.GetCancelRequestedAt() != nil {
		return status.Error(codes.FailedPrecondition, "only completed, non-cancelled training output can be recovered")
	}
	if s.outputControl == nil || s.outputControl.OutputStopped(ctx, op) != nil {
		return status.Error(codes.FailedPrecondition, "training Job stop is unconfirmed; retry after it exits")
	}
	return nil
}

func (s *Service) GetTrainingOutput(ctx context.Context, req *trainingv1.GetTrainingRunRequest) (*trainingv1.TrainingOutput, error) {
	id, e := trainingID(req.GetTrainingRunId())
	if e != nil {
		return nil, e
	}
	run, op, dir, e := s.outputIdentity(ctx, id)
	if e != nil {
		return nil, e
	}
	out := &trainingv1.TrainingOutput{TrainingRunId: id, OperationId: run.GetOperationId(), Availability: "not_retained", RecoveryStatus: "none", OutputModelVersionId: run.GetOutputModelVersionId()}
	if dir != "" {
		r, readErr := trainingoutput.Read(dir)
		if readErr == nil {
			if validateSavedOutput(r, run, op, s.outputNode) != nil {
				out.Availability = "invalid"
			} else {
				m, _ := r.Validate()
				out.Availability = "retained"
				out.ManifestSha256 = r.Digest
				out.FileCount = int64(m.FileCount)
				out.SizeBytes = m.TotalBytes
				if _, err := os.Stat(filepath.Join(dir, "released")); err == nil {
					out.Availability = "released"
				}
			}
		} else if os.IsNotExist(readErr) {
			if _, err := os.Stat(dir); err == nil {
				out.Availability = "unsealed"
			}
		} else {
			out.Availability = "invalid"
		}
	}
	var created, updated time.Time
	var recovered string
	e = s.pool.QueryRow(ctx, `SELECT recovery_id,status,error_code,output_model_version_id,requested_at,updated_at FROM training_output_recoveries WHERE training_run_id=$1 ORDER BY requested_at DESC,recovery_id DESC LIMIT 1`, id).Scan(&out.RecoveryId, &out.RecoveryStatus, &out.ErrorCode, &recovered, &created, &updated)
	if e == nil {
		out.RequestedAt = timestamppb.New(created)
		out.UpdatedAt = timestamppb.New(updated)
		if recovered != "" {
			out.OutputModelVersionId = recovered
		}
	} else if !errors.Is(e, pgx.ErrNoRows) {
		return nil, idempotency.DBError(e)
	}
	return out, nil
}

func (s *Service) RetryTrainingOutput(ctx context.Context, req *trainingv1.RetryTrainingOutputRequest) (*trainingv1.TrainingOutput, error) {
	id, e := trainingID(req.GetTrainingRunId())
	if e != nil {
		return nil, e
	}
	if req.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.InvalidArgument, "Idempotency-Key is required")
	}
	if e = idempotency.ValidateKey(req.GetIdempotencyKey()); e != nil {
		return nil, e
	}
	// Existing keys are replayed before checking local files, so cleanup does not
	// turn a previously accepted request into a new attempt or a missing-file error.
	var replay bool
	if e = s.pool.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM training_output_recoveries WHERE training_run_id=$1 AND idempotency_key=$2)`, id, artifacts.SHA256([]byte(req.GetIdempotencyKey()))).Scan(&replay); e != nil {
		return nil, idempotency.DBError(e)
	}
	if replay {
		return s.outputReplay(ctx, id, req.GetIdempotencyKey())
	}
	run, op, dir, e := s.outputIdentity(ctx, id)
	if e != nil {
		return nil, e
	}
	if e = s.requireStoppedOutput(ctx, run, op); e != nil {
		return nil, e
	}
	if s.outputStore == nil {
		return nil, status.Error(codes.Unavailable, "output publisher unavailable")
	}
	r, e := trainingoutput.Read(dir)
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, "completed output was not retained; upload-only recovery is unavailable")
	}
	if validateSavedOutput(r, run, op, s.outputNode) != nil {
		return nil, status.Error(codes.FailedPrecondition, "saved output provenance mismatch")
	}
	if _, e = os.Stat(filepath.Join(dir, "released")); e == nil {
		return nil, status.Error(codes.FailedPrecondition, "local output has already been released")
	}
	tx, e := s.pool.Begin(ctx)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	defer tx.Rollback(ctx)
	if e = purge.RequireAvailable(ctx, tx, run.GetSourceModelVersionId()); e != nil {
		return nil, e
	}
	// Serialize request creation. Workers never hold this row during S3 I/O.
	var state string
	if e = tx.QueryRow(ctx, `SELECT status FROM training_runs WHERE training_run_id=$1 FOR UPDATE`, id).Scan(&state); e != nil {
		return nil, idempotency.DBError(e)
	}
	if state != run.GetStatus() {
		return nil, status.Error(codes.FailedPrecondition, "training state changed")
	}
	if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM training_output_recoveries WHERE training_run_id=$1 AND idempotency_key=$2)`, id, artifacts.SHA256([]byte(req.GetIdempotencyKey()))).Scan(&replay); e != nil {
		return nil, idempotency.DBError(e)
	}
	if !replay {
		unlock, lockErr := trainingoutput.Lock(dir)
		if lockErr != nil {
			return nil, status.Error(codes.FailedPrecondition, "saved output busy; retry the same key")
		}
		defer unlock()
		if _, err := os.Stat(filepath.Join(dir, "released")); err == nil {
			return nil, status.Error(codes.FailedPrecondition, "local output has already been released")
		}
		var active bool
		if e = tx.QueryRow(ctx, `SELECT EXISTS(SELECT 1 FROM training_output_recoveries WHERE training_run_id=$1 AND status IN ('queued','running'))`, id).Scan(&active); e != nil {
			return nil, idempotency.DBError(e)
		}
		if active {
			return nil, status.Error(codes.FailedPrecondition, "an output recovery is already active; poll the existing request")
		}
		_, e = tx.Exec(ctx, `INSERT INTO training_output_recoveries(recovery_id,training_run_id,idempotency_key,manifest_sha256,status) VALUES($1,$2,$3,$4,'queued')`, uuid.NewString(), id, artifacts.SHA256([]byte(req.GetIdempotencyKey())), r.Digest)
		if e != nil {
			return nil, idempotency.DBError(e)
		}
	}
	if e = tx.Commit(ctx); e != nil {
		return nil, idempotency.DBError(e)
	}
	return s.outputReplay(ctx, id, req.GetIdempotencyKey())
}

func (s *Service) outputReplay(ctx context.Context, id, key string) (*trainingv1.TrainingOutput, error) {
	out, e := s.GetTrainingOutput(ctx, &trainingv1.GetTrainingRunRequest{TrainingRunId: id})
	if e != nil {
		return nil, e
	}
	var created, updated time.Time
	var outputID string
	e = s.pool.QueryRow(ctx, `SELECT recovery_id,status,error_code,output_model_version_id,requested_at,updated_at FROM training_output_recoveries WHERE training_run_id=$1 AND idempotency_key=$2`, id, artifacts.SHA256([]byte(key))).Scan(&out.RecoveryId, &out.RecoveryStatus, &out.ErrorCode, &outputID, &created, &updated)
	if e != nil {
		return nil, idempotency.DBError(e)
	}
	out.OutputModelVersionId = outputID
	out.RequestedAt = timestamppb.New(created)
	out.UpdatedAt = timestamppb.New(updated)
	return out, nil
}

// ProcessOutputRecovery resumes queued or interrupted publication under a
// filesystem lock. It creates no Operation, invokes no trainer and uses no GPU.
func (s *Service) ProcessOutputRecovery(ctx context.Context, id string) error {
	var recoveryID, runID, digest string
	e := s.pool.QueryRow(ctx, `SELECT recovery_id,training_run_id,manifest_sha256 FROM training_output_recoveries WHERE status IN ('queued','running') AND ($1::text='' OR training_run_id::text=$1) ORDER BY updated_at,recovery_id LIMIT 1`, id).Scan(&recoveryID, &runID, &digest)
	if errors.Is(e, pgx.ErrNoRows) {
		return nil
	}
	if e != nil {
		return e
	}
	// Rotate observations so an unavailable Operation cannot starve other runs.
	if _, e = s.pool.Exec(ctx, `UPDATE training_output_recoveries SET updated_at=clock_timestamp() WHERE recovery_id=$1 AND status IN ('queued','running')`, recoveryID); e != nil {
		return e
	}
	run, op, dir, e := s.outputIdentity(ctx, runID)
	if e != nil {
		return e
	}
	if dir == "" {
		return s.finishRecovery(ctx, recoveryID, "SAVED_OUTPUT_UNAVAILABLE", nil, "")
	}
	unlock, e := trainingoutput.Lock(dir)
	if e != nil {
		if os.IsNotExist(e) {
			return s.finishRecovery(ctx, recoveryID, "SAVED_OUTPUT_UNAVAILABLE", nil, "")
		}
		return e
	}
	defer unlock()
	tag, e := s.pool.Exec(ctx, `UPDATE training_output_recoveries SET status='running',updated_at=clock_timestamp() WHERE recovery_id=$1 AND status IN ('queued','running')`, recoveryID)
	if e != nil || tag.RowsAffected() == 0 {
		return e
	}
	code := ""
	var published *managementv1.ModelVersion
	result := ""
	r, readErr := trainingoutput.Read(dir)
	if e = s.requireStoppedOutput(ctx, run, op); e != nil {
		code = "OUTPUT_PRODUCER_NOT_STOPPED"
	} else if readErr != nil || r.Digest != digest || validateSavedOutput(r, run, op, s.outputNode) != nil {
		code = "SAVED_OUTPUT_INVALID"
	} else if e = trainingoutput.Verify(dir, r); e != nil {
		code = "SAVED_OUTPUT_CHANGED"
	} else {
		var store trainingoutput.Store
		if s.outputStore == nil {
			code = "OUTPUT_STORE_UNAVAILABLE"
		} else if store, e = s.outputStore(ctx); e != nil {
			code = "OUTPUT_STORE_UNAVAILABLE"
		} else {
			m, _ := r.Validate()
			published, e = trainingoutput.Publish(ctx, s.clients.Management, store, runID, r.SourceID, filepath.Join(dir, "output"), m, r.Manifest, r.Digest, nil)
			if e != nil {
				code = "OUTPUT_PUBLICATION_FAILED"
			} else {
				fields := map[string]json.RawMessage{}
				e = json.Unmarshal(r.Result, &fields)
				for name, value := range map[string]any{"output_model_version_id": published.GetModelVersionId(), "output_artifact_uri": published.GetArtifactUri(), "output_manifest_sha256": r.Digest, "output_artifact_kind": "adapter"} {
					fields[name], _ = json.Marshal(value)
				}
				raw, marshalErr := json.Marshal(fields)
				if e != nil || marshalErr != nil {
					code = "SAVED_RESULT_INVALID"
				} else {
					result = string(raw)
				}
			}
		}
	}
	// Process death or a DB outage leaves 'running' for restart continuation.
	// A known attempt timeout stops publication retries until a new request key.
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		finishCtx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		return s.finishRecovery(finishCtx, recoveryID, "OUTPUT_PUBLICATION_TIMEOUT", nil, "")
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	return s.finishRecovery(ctx, recoveryID, code, published, result)
}
func (s *Service) finishRecovery(ctx context.Context, id, code string, mv *managementv1.ModelVersion, result string) error {
	state := "succeeded"
	outputID := mv.GetModelVersionId()
	if code != "" {
		state = "failed"
		outputID = ""
		result = ""
	}
	_, e := s.pool.Exec(ctx, `UPDATE training_output_recoveries SET status=$2,error_code=$3,output_model_version_id=$4,result_json=$5,updated_at=clock_timestamp() WHERE recovery_id=$1 AND status IN ('queued','running')`, id, state, code, outputID, result)
	if e == nil {
		log.Printf("training output recovery id=%s status=%s code=%s", id, state, code)
	}
	return e
}
func (s *Service) RunOutputRecoveries(ctx context.Context) {
	log.Print("training output recovery controller started (local output; no retraining)")
	for ctx.Err() == nil {
		c, cancel := context.WithTimeout(ctx, 20*time.Minute)
		e := s.ProcessOutputRecovery(c, "")
		cancel()
		if e != nil {
			log.Print("training output recovery cycle pending")
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

// Explicit cleanup verifies the remote artifact again, retains the receipt and
// DB audit trail, and removes only this completed output directory.
func (s *Service) ReleaseTrainingOutput(ctx context.Context, req *trainingv1.GetTrainingRunRequest) (*trainingv1.TrainingOutput, error) {
	id, e := trainingID(req.GetTrainingRunId())
	if e != nil {
		return nil, e
	}
	run, op, dir, e := s.outputIdentity(ctx, id)
	if e != nil {
		return nil, e
	}
	if e = s.requireStoppedOutput(ctx, run, op); e != nil {
		return nil, e
	}
	unlock, e := trainingoutput.Lock(dir)
	if e != nil {
		return nil, status.Error(codes.FailedPrecondition, "saved output busy or unavailable")
	}
	defer unlock()
	out, e := s.GetTrainingOutput(ctx, req)
	if e != nil {
		return nil, e
	}
	if out.GetAvailability() == "released" {
		return out, nil
	}
	if out.GetRecoveryStatus() == "queued" || out.GetRecoveryStatus() == "running" {
		return nil, status.Error(codes.FailedPrecondition, "output recovery is active")
	}
	r, e := trainingoutput.Read(dir)
	if e != nil || validateSavedOutput(r, run, op, s.outputNode) != nil {
		return nil, status.Error(codes.FailedPrecondition, "saved output provenance unavailable")
	}
	if out.GetOutputModelVersionId() == "" || s.outputStore == nil {
		return nil, status.Error(codes.FailedPrecondition, "verified ready output is required before cleanup")
	}
	mv, e := s.clients.Management.GetModelVersion(ctx, &managementv1.GetModelVersionRequest{ModelVersionId: out.GetOutputModelVersionId()})
	if e != nil || mv.GetState() != "ready" || mv.GetTrainingRunId() != id || mv.GetSourceModelVersionId() != r.SourceID || mv.GetArtifactManifestSha256() != r.Digest {
		return nil, status.Error(codes.FailedPrecondition, "ready output identity mismatch")
	}
	store, e := s.outputStore(ctx)
	if e != nil {
		return nil, status.Error(codes.Unavailable, "output store unavailable")
	}
	prefix, e := artifacts.Prefix(mv.GetModelId(), mv.GetModelVersionId(), r.Digest)
	if e != nil {
		return nil, e
	}
	m, e := store.VerifyManifest(ctx, prefix, r.Digest)
	if e != nil || mv.GetArtifactUri() != store.URI(prefix) || int64(m.FileCount) != out.GetFileCount() || m.TotalBytes != out.GetSizeBytes() {
		return nil, status.Error(codes.FailedPrecondition, "remote output verification failed; local files retained")
	}
	if e = ctx.Err(); e != nil {
		return nil, e
	}
	if e = os.RemoveAll(filepath.Join(dir, "output")); e != nil {
		return nil, status.Error(codes.Internal, "local output cleanup incomplete; retry cleanup")
	}
	if e = os.WriteFile(filepath.Join(dir, "released"), []byte(fmt.Sprintf("%s\n", mv.GetModelVersionId())), 0600); e != nil {
		return nil, status.Error(codes.Internal, "cleanup receipt pending; retry cleanup")
	}
	return s.GetTrainingOutput(ctx, req)
}
