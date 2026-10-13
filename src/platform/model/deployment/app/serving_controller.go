package app

import (
	"context"
	"encoding/json"
	"errors"
	"log"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"
	serving "model-platform/deployment/runtime"
	"model-platform/internal/gpuadmission"
)

type servingController struct {
	service *Service
	config  serving.Config
	driver  serving.Driver
	fetcher serving.Fetcher
}
type servingRow struct {
	plan                               serving.Plan
	desired, apply, state, containerID string
	authorized                         bool
	launchExpired                      bool
}

func loadServing(ctx context.Context, conn *pgxpool.Conn, id string) (servingRow, error) {
	var r servingRow
	var spec, source, resources []byte
	var engine string
	r.plan.DeploymentID = id
	err := conn.QueryRow(ctx, `SELECT d.engine,q.operation_id,d.serving_spec,d.serving_source,d.resources,d.runtime_image_id,d.desired_state,d.apply_status,d.runtime_state,d.runtime_container_id,d.runtime_authorized,COALESCE(d.runtime_launch_at<clock_timestamp()-interval '15 minutes',FALSE) FROM deployments d JOIN operation_submissions q ON q.resource_id=d.deployment_id AND q.resource_type='deployment' WHERE d.deployment_id=$1 AND d.deleted_at IS NULL AND d.engine IN ('vllm','vllm-lora','vllm-surya')`, id).Scan(&engine, &r.plan.OperationID, &spec, &source, &resources, &r.plan.ImageID, &r.desired, &r.apply, &r.state, &r.containerID, &r.authorized, &r.launchExpired)
	if err != nil {
		return r, err
	}
	for _, item := range []struct {
		raw  []byte
		dest any
	}{{spec, &r.plan.Spec}, {source, &r.plan.Source}, {resources, &r.plan.Resources}} {
		if err := json.Unmarshal(item.raw, item.dest); err != nil {
			return r, err
		}
	}
	if (engine == "vllm-surya") != (r.plan.Spec.Profile == serving.SuryaProfile) {
		return r, errors.New("stored serving profile mismatch")
	}
	if (engine == "vllm-lora") != (r.plan.Source.Adapter != nil) {
		return r, errors.New("stored serving engine/source mismatch")
	}
	if r.plan.Source.Adapter != nil && !serving.SupportedAdapterServingFormat(r.plan.Source.Adapter.ServingFormat) {
		return r, errors.New("unsupported adapter serving format")
	}
	return r, nil
}

// RunOnce is exposed for isolated tests and explicit controller verification.
// Runtime creation is gated by the persisted StartServing authorization.
func (s *Service) RunServingOnce(ctx context.Context, id string) error {
	if s.runtime == nil {
		return errors.New("serving is not configured")
	}
	return s.runtime.process(ctx, id)
}
func (w *servingController) run(ctx context.Context) {
	log.Print("deployment serving controller started (single GPU; no automatic runtime restart)")
	for ctx.Err() == nil {
		rows, err := w.service.pool.Query(ctx, `SELECT deployment_id FROM deployments WHERE deleted_at IS NULL AND engine IN ('vllm','vllm-lora','vllm-surya') AND (runtime_authorized OR desired_state='stopped') ORDER BY (desired_state='stopped' AND status<>'stopped') DESC,runtime_checked_at NULLS FIRST,deployment_id LIMIT 20`)
		ids := []string{}
		if err == nil {
			for rows.Next() {
				var id string
				if e := rows.Scan(&id); e != nil {
					err = e
					break
				}
				ids = append(ids, id)
			}
			if e := rows.Err(); e != nil {
				err = e
			}
			rows.Close()
		}
		if err != nil {
			log.Print("serving controller candidate lookup unavailable")
		} else {
			for _, id := range ids {
				if err := w.process(ctx, id); err != nil {
					log.Printf("serving observation unavailable deployment=%s", id)
				}
			}
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(2 * time.Second):
		}
	}
}

func observe(ctx context.Context, conn *pgxpool.Conn, id, state, code, message string) error {
	if len(message) > 1000 {
		message = message[:1000]
	}
	_, err := conn.Exec(ctx, `UPDATE deployments SET runtime_state=$2,runtime_error_code=$3,runtime_error_message=$4,runtime_checked_at=clock_timestamp() WHERE deployment_id=$1`, id, state, code, message)
	return err
}
func failProvision(ctx context.Context, conn *pgxpool.Conn, id, code, message string) error {
	if len(message) > 1000 {
		message = message[:1000]
	}
	_, err := conn.Exec(ctx, `UPDATE deployments SET apply_status=CASE WHEN apply_status='ready' THEN apply_status ELSE 'failed' END,status=CASE WHEN desired_state='stopped' THEN 'stopping' ELSE 'failed' END,runtime_state='failed',runtime_endpoint='',runtime_error_code=$2,runtime_error_message=$3,runtime_checked_at=clock_timestamp(),updated_at=clock_timestamp() WHERE deployment_id=$1`, id, code, message)
	return err
}

func (w *servingController) process(parent context.Context, id string) error {
	id, err := deploymentID(id)
	if err != nil {
		return err
	}
	// One session lock protects this deployment across controller replicas. API
	// stop writes the desired state independently and cancels a long download.
	conn, err := w.service.pool.Acquire(parent)
	if err != nil {
		return err
	}
	defer conn.Release()
	var locked bool
	if err := conn.QueryRow(parent, `SELECT pg_try_advisory_lock(hashtextextended($1::text,0))`, "serving:"+id).Scan(&locked); err != nil {
		return err
	}
	if !locked {
		return nil
	}
	defer func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		var released bool
		err := conn.QueryRow(ctx, `SELECT pg_advisory_unlock(hashtextextended($1::text,0))`, "serving:"+id).Scan(&released)
		// Never return a session carrying an advisory lock to the pool.
		if err != nil || !released {
			_ = conn.Conn().Close(ctx)
		}
	}()
	ctx, cancel := context.WithTimeout(parent, 35*time.Minute)
	defer cancel()
	r, err := loadServing(ctx, conn, id)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil
	}
	if err != nil {
		return err
	}
	if r.desired == "stopped" {
		return w.stop(ctx, conn, r)
	}
	if !r.authorized {
		return nil
	}
	if r.apply == "failed" {
		_, err := conn.Exec(ctx, `UPDATE deployments SET runtime_checked_at=clock_timestamp() WHERE deployment_id=$1`, id)
		return err
	}

	if r.plan.ImageID == "" {
		pinned, err := serving.ResolveImage(ctx, w.driver, r.plan.Spec.Profile)
		if err != nil {
			return failProvision(ctx, conn, id, "SERVING_IMAGE_UNAVAILABLE", err.Error())
		}
		r.plan.ImageID = pinned
		if _, err := conn.Exec(ctx, `UPDATE deployments SET runtime_image_id=$2,runtime_state='preparing',runtime_checked_at=clock_timestamp() WHERE deployment_id=$1 AND desired_state='running'`, id, pinned); err != nil {
			return err
		}
		// If stop won the update, no runtime work is permitted below.
		r, err = loadServing(ctx, conn, id)
		if err != nil {
			return err
		}
		if r.desired == "stopped" {
			return w.stop(ctx, conn, r)
		}
	}
	e, err := w.driver.Inspect(ctx, r.plan)
	if err != nil {
		return observe(ctx, conn, id, "unknown", "RUNTIME_INSPECT_UNAVAILABLE", err.Error())
	}
	if e == nil {
		if r.containerID != "" {
			return failProvision(ctx, conn, id, "RUNTIME_MISSING", "recorded runtime is missing; automatic recreation is disabled")
		}
		if r.apply == "ready" {
			return failProvision(ctx, conn, id, "RUNTIME_MISSING", "previously ready runtime is missing; automatic recreation is disabled")
		}
		if err := observe(ctx, conn, id, "preparing", "", ""); err != nil {
			return err
		}
		// Cancellation watcher uses another connection, keeping the session
		// connection exclusively owned by this controller invocation.
		prepareCtx, prepareCancel := context.WithTimeout(ctx, 30*time.Minute)
		watchDone := make(chan struct{})
		go func() {
			defer close(watchDone)
			ticker := time.NewTicker(time.Second)
			defer ticker.Stop()
			for {
				select {
				case <-prepareCtx.Done():
					return
				case <-ticker.C:
					checkCtx, checkCancel := context.WithTimeout(prepareCtx, 2*time.Second)
					var desired string
					err := w.service.pool.QueryRow(checkCtx, `SELECT desired_state FROM deployments WHERE deployment_id=$1`, id).Scan(&desired)
					checkCancel()
					// Losing the control DB cannot authorize further materialization.
					if err != nil || desired != "running" {
						prepareCancel()
						return
					}
				}
			}
		}()
		prepareErr := w.fetcher.Prepare(prepareCtx, r.plan)
		prepareCancel()
		<-watchDone
		r, err = loadServing(ctx, conn, id)
		if err != nil {
			return err
		}
		if r.desired == "stopped" {
			return w.stop(ctx, conn, r)
		}
		if prepareErr != nil {
			code := "SNAPSHOT_PREPARATION_FAILED"
			if r.plan.Source.Adapter != nil {
				code = "ADAPTER_PREPARATION_FAILED"
			}
			return failProvision(ctx, conn, id, code, prepareErr.Error())
		}
		if err := w.driver.Create(ctx, r.plan); err != nil {
			var busy *gpuadmission.BusyError
			if errors.As(err, &busy) {
				return observe(ctx, conn, id, "waiting_resources", "GPU_BUSY", err.Error())
			}
			// The Docker daemon may have accepted create before the reply was
			// lost. Keep intent and inspect the deterministic name next cycle.
			return observe(ctx, conn, id, "unknown", "RUNTIME_CREATE_UNCONFIRMED", err.Error())
		}
		e, err = w.driver.Inspect(ctx, r.plan)
		if err != nil || e == nil {
			return observe(ctx, conn, id, "unknown", "RUNTIME_CREATE_UNCONFIRMED", "created container could not be confirmed")
		}
	}
	if r.containerID != "" && r.containerID != e.ID {
		return observe(ctx, conn, id, "unknown", "RUNTIME_ID_MISMATCH", "runtime container changed; automatic adoption is disabled")
	}
	if r.containerID == "" {
		if _, err := conn.Exec(ctx, `UPDATE deployments SET runtime_container_id=$2 WHERE deployment_id=$1`, id, e.ID); err != nil {
			return err
		}
		r.containerID = e.ID
	}
	r, err = loadServing(ctx, conn, id)
	if err != nil {
		return err
	}
	if r.desired == "stopped" {
		return w.stop(ctx, conn, r)
	}
	if e.State.Status == "created" && r.apply != "ready" {
		if _, err := conn.Exec(ctx, `UPDATE deployments SET runtime_launch_at=COALESCE(runtime_launch_at,clock_timestamp()),runtime_state='starting',runtime_checked_at=clock_timestamp() WHERE deployment_id=$1`, id); err != nil {
			return err
		}
		if err := w.driver.Start(ctx, e.ID); err != nil {
			var busy *gpuadmission.BusyError
			if errors.As(err, &busy) {
				return observe(ctx, conn, id, "waiting_resources", "GPU_BUSY", err.Error())
			}
			return observe(ctx, conn, id, "unknown", "RUNTIME_START_UNCONFIRMED", err.Error())
		}
		// Re-observe; never mark ready just because docker start returned zero.
		return nil
	}
	if !e.State.Running || e.State.Paused || e.State.Restarting {
		if r.apply != "ready" {
			code, message := "RUNTIME_NOT_RUNNING", "vLLM exited or is not runnable; inspect its Docker logs"
			if diagnostic, ok := w.driver.(interface {
				Failure(context.Context, *serving.Evidence) (string, string)
			}); ok {
				code, message = diagnostic.Failure(ctx, e)
			}
			return failProvision(ctx, conn, id, code, message)
		}
		_, err := conn.Exec(ctx, `UPDATE deployments SET status=CASE WHEN desired_state='stopped' THEN 'stopping' ELSE 'unhealthy' END,runtime_state='exited',runtime_endpoint='',runtime_error_code='RUNTIME_NOT_RUNNING',runtime_error_message='runtime is not running; automatic restart is disabled',runtime_checked_at=clock_timestamp() WHERE deployment_id=$1`, id)
		return err
	}
	healthy, healthErr := w.driver.Healthy(ctx, r.plan)
	if !healthy {
		if r.apply != "ready" && r.launchExpired {
			// Preserve container/logs for diagnosis. Stop is an explicit API.
			return failProvision(ctx, conn, id, "READINESS_TIMEOUT", "vLLM did not become healthy within 15 minutes; inspect logs and stop the runtime")
		}
		state := "starting"
		code := ""
		message := ""
		if r.apply == "ready" {
			state = "unhealthy"
			code = "HEALTH_CHECK_FAILED"
		}
		if healthErr != nil {
			message = healthErr.Error()
		}
		if err := observe(ctx, conn, id, state, code, message); err != nil {
			return err
		}
		if r.apply == "ready" {
			_, err = conn.Exec(ctx, `UPDATE deployments SET status=CASE WHEN desired_state='stopped' THEN 'stopping' ELSE 'unhealthy' END,runtime_endpoint='' WHERE deployment_id=$1`, id)
		}
		return err
	}
	endpoint, err := serving.Endpoint(e)
	if err != nil {
		return failProvision(ctx, conn, id, "RUNTIME_ENDPOINT_INVALID", err.Error())
	}
	_, err = conn.Exec(ctx, `UPDATE deployments SET status='ready',apply_status='ready',runtime_state='ready',runtime_endpoint=$2,runtime_error_code='',runtime_error_message='',runtime_checked_at=clock_timestamp(),updated_at=clock_timestamp() WHERE deployment_id=$1 AND desired_state='running' AND runtime_container_id=$3`, id, endpoint, e.ID)
	return err
}

func (w *servingController) stop(ctx context.Context, conn *pgxpool.Conn, r servingRow) error {
	e, err := w.driver.Inspect(ctx, r.plan)
	if err != nil {
		return observe(ctx, conn, r.plan.DeploymentID, "unknown", "STOP_INSPECT_UNAVAILABLE", err.Error())
	}
	if e != nil {
		if r.containerID != "" && r.containerID != e.ID {
			return observe(ctx, conn, r.plan.DeploymentID, "unknown", "STOP_ID_MISMATCH", "refusing to stop a different runtime")
		}
		if e.State.Running || e.State.Paused || e.State.Restarting {
			if err := w.driver.Stop(ctx, e.ID); err != nil {
				return observe(ctx, conn, r.plan.DeploymentID, "stopping", "STOP_UNCONFIRMED", err.Error())
			}
			again, err := w.driver.Inspect(ctx, r.plan)
			if err != nil || again != nil && (again.ID != e.ID || again.State.Running || again.State.Paused || again.State.Restarting) {
				return observe(ctx, conn, r.plan.DeploymentID, "stopping", "STOP_UNCONFIRMED", "runtime stop could not be confirmed")
			}
		}
	}
	// desired_state was durably stopped before this invocation. Release absent
	// create intent too; the controller keeps observing stopped owners so any
	// delayed never-started container is removed on a later pass.
	if releaser, ok := w.driver.(interface {
		ReleaseGPU(context.Context, serving.Plan) error
	}); ok {
		if err := releaser.ReleaseGPU(ctx, r.plan); err != nil {
			return observe(ctx, conn, r.plan.DeploymentID, "stopping", "GPU_RELEASE_UNCONFIRMED", err.Error())
		}
	}
	_, err = conn.Exec(ctx, `UPDATE deployments SET status='stopped',apply_status=CASE WHEN apply_status='ready' THEN 'ready' ELSE 'failed' END,runtime_state='stopped',runtime_endpoint='',runtime_error_code='',runtime_error_message='',runtime_checked_at=clock_timestamp(),updated_at=clock_timestamp() WHERE deployment_id=$1 AND desired_state='stopped'`, r.plan.DeploymentID)
	return err
}
