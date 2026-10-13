package app

// Terminal Job cleanup runs separately from reconciliation. It never repairs
// state, kills a process, removes a volume, or interprets exit 0 as API success.
import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"time"

	"knowledge-platform/dispatcher/executor"
	operationv1 "knowledge-platform/gen/operation/v1"
	"knowledge-platform/internal/execution"
	"github.com/google/uuid"
)

var cleanupContainerID = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Never fetch Config.Env: it contains credentials used by the finished Job.
const cleanupInspectFormat = `{"ID":{{json .Id}},"Name":{{json .Name}},"Image":{{json .Image}},"Labels":{{json .Config.Labels}},"RestartCount":{{json .RestartCount}},"AutoRemove":{{json .HostConfig.AutoRemove}},"RestartPolicy":{{json .HostConfig.RestartPolicy.Name}},"Networks":{{json .NetworkSettings.Networks}},"State":{{json .State}}}`

type cleanupRuntime struct {
	ID, Name, Image, RestartPolicy string
	Labels                         map[string]string
	Networks                       map[string]json.RawMessage
	RestartCount                   int
	AutoRemove                     bool
	State                          struct {
		Status                      string
		Running, Paused, Restarting bool
		OOMKilled                   bool
		ExitCode                    int
		StartedAt, FinishedAt       time.Time
	}
}

type cleanupSnapshot struct {
	Runtime                               cleanupRuntime
	OperationID, Token, Status, ErrorCode string
	CompletedAt                           time.Time
}

type JobCleanup struct {
	Operations               operationv1.OperationServiceClient
	Call                     executor.CommandFunc
	Mode, Network            string
	SuccessKeep, FailureKeep time.Duration
	Interval                 time.Duration
	Now                      func() time.Time
	cursor                   string
}

func cleanupUUID(value string) bool {
	u, err := uuid.Parse(value)
	return err == nil && u != uuid.Nil && u.String() == value
}

func (c *JobCleanup) Validate() error {
	if c.Mode != "off" && c.Mode != "dry-run" && c.Mode != "apply" {
		return fmt.Errorf("KNOWLEDGE_JOB_CLEANUP_MODE must be off, dry-run or apply")
	}
	if c.Network == "" || c.SuccessKeep < time.Second || c.FailureKeep < time.Second || c.Interval < time.Second {
		return fmt.Errorf("Job cleanup requires a network and positive durations")
	}
	return nil
}

func (c *JobCleanup) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

func (c *JobCleanup) snapshot(ctx context.Context, id string) (cleanupSnapshot, error) {
	var s cleanupSnapshot
	if !cleanupContainerID.MatchString(id) {
		return s, fmt.Errorf("invalid full container ID")
	}
	raw, err := c.Call(ctx, "container", "inspect", "--format", cleanupInspectFormat, id)
	if err != nil || json.Unmarshal(raw, &s.Runtime) != nil {
		return s, fmt.Errorf("runtime observation unavailable")
	}
	r := s.Runtime
	l := r.Labels
	prefix := "knowledge-platform."
	idOp, token, resource := l[prefix+"operation_id"], l[prefix+"dispatch_token"], l[prefix+"resource_id"]
	if r.ID != id || l[prefix+"owner"] != "dispatcher" || l[prefix+"execution-gate"] != "1" ||
		!cleanupUUID(idOp) || !cleanupUUID(token) || !cleanupUUID(resource) ||
		r.Name != "/"+executor.Name(idOp, token) || !strings.HasPrefix(r.Image, "sha256:") ||
		!cleanupContainerID.MatchString(strings.TrimPrefix(r.Image, "sha256:")) {
		return s, fmt.Errorf("runtime ownership/identity mismatch")
	}
	if r.State.Status != "exited" || r.State.Running || r.State.Paused || r.State.Restarting ||
		r.RestartCount != 0 || r.AutoRemove || (r.RestartPolicy != "" && r.RestartPolicy != "no") {
		return s, fmt.Errorf("runtime is not a stopped one-shot Job")
	}
	if _, ok := r.Networks[c.Network]; !ok {
		return s, fmt.Errorf("runtime network mismatch")
	}
	now := c.now()
	if r.State.StartedAt.IsZero() || r.State.FinishedAt.IsZero() ||
		r.State.FinishedAt.Before(r.State.StartedAt) || r.State.FinishedAt.After(now) {
		return s, fmt.Errorf("invalid runtime timestamps")
	}
	op, err := c.Operations.GetOperation(ctx, &operationv1.GetOperationRequest{OperationId: idOp})
	if err != nil || op == nil {
		return s, fmt.Errorf("operation observation unavailable")
	}
	if op.OperationId != idOp || op.DispatchToken != token || op.ResourceId != resource ||
		op.ExecutionType != l[prefix+"execution_type"] || !execution.Supported(op.ExecutionType, op.ResourceType) ||
		op.DispatchProtocol != 1 || !cleanupUUID(op.ExecutionInstanceId) || op.GetResources().GetNodeName() != "" ||
		op.GetResources().GetGpuCount() != 0 || len(op.GetResources().GetGpuIds()) != 0 {
		return s, fmt.Errorf("operation execution identity mismatch")
	}
	if op.Status != "succeeded" && op.Status != "failed" && op.Status != "cancelled" {
		return s, fmt.Errorf("operation is not terminal")
	}
	if op.ExecutionStartedAt == nil || op.ExecutionStartedAt.CheckValid() != nil ||
		op.CompletedAt == nil || op.CompletedAt.CheckValid() != nil || op.CompletedAt.AsTime().After(now) ||
		op.CompletedAt.AsTime().Before(op.ExecutionStartedAt.AsTime()) {
		return s, fmt.Errorf("execution/completion timestamps unconfirmed")
	}
	s.OperationID, s.Token, s.Status, s.ErrorCode = op.OperationId, op.DispatchToken, op.Status, op.ErrorCode
	s.CompletedAt = op.CompletedAt.AsTime()
	return s, nil
}

func (c *JobCleanup) due(s cleanupSnapshot) bool {
	end := s.Runtime.State.FinishedAt
	if s.CompletedAt.After(end) {
		end = s.CompletedAt
	}
	keep := c.FailureKeep
	if s.Status == "succeeded" && s.Runtime.State.ExitCode == 0 && !s.Runtime.State.OOMKilled {
		keep = c.SuccessKeep
	}
	return !c.now().Before(end.Add(keep))
}

func (c *JobCleanup) process(ctx context.Context, id string) (string, error) {
	if err := c.Validate(); err != nil {
		return "retained", err
	}
	if c.Mode == "off" {
		return "retained", nil
	}
	s, err := c.snapshot(ctx, id)
	if err != nil || !c.due(s) {
		return "retained", err
	}
	if c.Mode == "dry-run" {
		return "would_remove", nil
	}
	current, err := c.snapshot(ctx, id)
	if err != nil || !reflect.DeepEqual(current, s) || !c.due(current) {
		return "retained", fmt.Errorf("runtime/operation changed before cleanup")
	}
	if err := ctx.Err(); err != nil {
		return "retained", err
	}
	// No force or volume option. A concurrently restarted container is refused.
	if _, err := c.Call(ctx, "rm", id); err != nil {
		return "retained", fmt.Errorf("container removal not confirmed")
	}
	log.Printf("job cleanup removed container=%s operation=%s status=%s exit=%d error_code=%s", id, s.OperationID, s.Status, s.Runtime.State.ExitCode, s.ErrorCode)
	return "removed", nil
}

func (c *JobCleanup) Tick(ctx context.Context) {
	if c.Validate() != nil || c.Mode == "off" {
		return
	}
	q, cancel := context.WithTimeout(ctx, 10*time.Second)
	raw, err := c.Call(q, "ps", "-aq", "--no-trunc", "--filter", "label=knowledge-platform.owner=dispatcher", "--filter", "status=exited")
	cancel()
	if err != nil {
		log.Printf("job cleanup listing unavailable; retained")
		return
	}
	ids := strings.Fields(string(raw))
	sort.Strings(ids)
	if len(ids) == 0 {
		return
	}
	start := sort.SearchStrings(ids, c.cursor)
	if start < len(ids) && ids[start] == c.cursor {
		start++
	}
	// Rotate across bounded batches so retained jobs do not starve later IDs.
	for n := 0; n < len(ids) && n < 20; n++ {
		id := ids[(start+n)%len(ids)]
		c.cursor = id
		q, cancel := context.WithTimeout(ctx, 10*time.Second)
		action, err := c.process(q, id)
		cancel()
		if err != nil {
			log.Printf("job cleanup retained container=%s reason=%v", id, err)
		} else if action == "would_remove" {
			log.Printf("job cleanup would_remove container=%s", id)
		}
		if ctx.Err() != nil {
			return
		}
	}
}

func (c *JobCleanup) Run(ctx context.Context) {
	if c.Validate() != nil || c.Mode == "off" {
		return
	}
	ticker := time.NewTicker(c.Interval)
	defer ticker.Stop()
	for {
		c.Tick(ctx)
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
		}
	}
}
