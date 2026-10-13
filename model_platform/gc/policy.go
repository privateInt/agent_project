package main

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"time"

	operationv1 "model-platform/gen/operation/v1"

	"google.golang.org/protobuf/types/known/timestamppb"
)

const labelPrefix = "model-platform."

var uuidRE = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
var containerRE = regexp.MustCompile(`^[0-9a-f]{64}$`)

// Inspect contains only the fields needed for collection, never Config.Env.
type Inspect struct {
	ID            string
	Name          string
	Image         string
	RestartCount  int
	Labels        map[string]string
	Tty           bool
	AutoRemove    bool
	RestartPolicy string
	LogDriver     string
	Networks      map[string]json.RawMessage
	State         struct {
		Status                        string
		Running, Paused, Restarting    bool
		OOMKilled                     bool
		ExitCode                      int
		StartedAt, FinishedAt          time.Time
	}
}

type Policy struct {
	Mode                     string
	Node, Network            string
	SuccessKeep, FailureKeep time.Duration
}

// Snapshot is also the archived metadata. No environment, commands, tokens
// other than the execution identity, or domain error messages are captured.
type Snapshot struct {
	SchemaVersion       int       `json:"schema_version"`
	OperationID         string    `json:"operation_id"`
	DispatchToken       string    `json:"dispatch_token"`
	ExecutionInstanceID string    `json:"execution_instance_id"`
	ExecutionType       string    `json:"execution_type"`
	ResourceType        string    `json:"resource_type"`
	ResourceID          string    `json:"resource_id"`
	Status              string    `json:"status"`
	CompletedAt         time.Time `json:"completed_at"`
	ContainerID         string    `json:"container_id"`
	ContainerName       string    `json:"container_name"`
	ImageID             string    `json:"image_id"`
	Node                string    `json:"node"`
	LogDriver           string    `json:"log_driver"`
	StartedAt           time.Time `json:"started_at"`
	FinishedAt          time.Time `json:"finished_at"`
	ExitCode            int       `json:"exit_code"`
	OOMKilled           bool      `json:"oom_killed"`
}

func validTimestamp(t *timestamppb.Timestamp) bool {
	return t != nil && t.CheckValid() == nil && !t.AsTime().IsZero()
}

func (p Policy) candidate(c Inspect, op *operationv1.Operation, now time.Time) (Snapshot, error) {
	var s Snapshot
	if !containerRE.MatchString(c.ID) || c.Labels[labelPrefix+"owner"] != "operation-dispatcher" {
		return s, fmt.Errorf("container ownership mismatch")
	}
	if c.Labels[labelPrefix+"smoke"] != "" || c.Labels[labelPrefix+"dispatch-fault-test"] != "" {
		return s, fmt.Errorf("test fixture excluded")
	}
	if op == nil || !uuidRE.MatchString(op.GetOperationId()) || !uuidRE.MatchString(op.GetDispatchToken()) || op.GetDispatchProtocol() != 1 {
		return s, fmt.Errorf("legacy or missing execution identity")
	}
	if !validTimestamp(op.GetExecutionStartedAt()) || op.GetExecutionStartedAt().AsTime().After(now) || !uuidRE.MatchString(op.GetExecutionInstanceId()) {
		return s, fmt.Errorf("execution grant not confirmed")
	}
	if c.Name != "/model-op-"+op.GetOperationId()+"-"+op.GetDispatchToken() ||
		c.Labels[labelPrefix+"operation_id"] != op.GetOperationId() ||
		c.Labels[labelPrefix+"dispatch_token"] != op.GetDispatchToken() ||
		c.Labels[labelPrefix+"execution_type"] != op.GetExecutionType() {
		return s, fmt.Errorf("container and current operation identity differ")
	}
	resourceTypes := map[string]string{
		"management.import-huggingface": "model_version",
		"training.run": "training_run",
		"deployment.apply": "deployment",
		"deployment.serve": "deployment",
	}
	if resourceTypes[op.GetExecutionType()] == "" || resourceTypes[op.GetExecutionType()] != op.GetResourceType() || !uuidRE.MatchString(op.GetResourceId()) {
		return s, fmt.Errorf("unsupported resource identity")
	}
	if op.GetStatus() != "succeeded" && op.GetStatus() != "failed" && op.GetStatus() != "cancelled" {
		return s, fmt.Errorf("operation is not terminal")
	}
	if !validTimestamp(op.GetCompletedAt()) || op.GetCompletedAt().AsTime().After(now) {
		return s, fmt.Errorf("invalid operation completion time")
	}
	if c.State.Status != "exited" || c.State.Running || c.State.Paused || c.State.Restarting || c.RestartCount != 0 || c.AutoRemove ||
		(c.RestartPolicy != "" && c.RestartPolicy != "no") {
		return s, fmt.Errorf("container must be exited with no restart policy or restarts")
	}
	if c.State.StartedAt.IsZero() || c.State.FinishedAt.IsZero() || c.State.FinishedAt.Before(c.State.StartedAt) || c.State.FinishedAt.After(now) {
		return s, fmt.Errorf("invalid container execution times")
	}
	if c.Tty || (c.LogDriver != "json-file" && c.LogDriver != "local" && c.LogDriver != "journald") {
		return s, fmt.Errorf("unsupported TTY or log driver")
	}
	requestedNode := op.GetResources().GetNodeName()
	if requestedNode != c.Labels[labelPrefix+"requested_node"] || (requestedNode != "" && requestedNode != p.Node) {
		return s, fmt.Errorf("node ownership mismatch")
	}
	if _, ok := c.Networks[p.Network]; !ok || p.Network == "" {
		return s, fmt.Errorf("job network mismatch")
	}
	return Snapshot{
		SchemaVersion: 1, OperationID: op.GetOperationId(), DispatchToken: op.GetDispatchToken(),
		ExecutionInstanceID: op.GetExecutionInstanceId(), ExecutionType: op.GetExecutionType(),
		ResourceType: op.GetResourceType(), ResourceID: op.GetResourceId(), Status: op.GetStatus(),
		CompletedAt: op.GetCompletedAt().AsTime().UTC(), ContainerID: c.ID, ContainerName: strings.TrimPrefix(c.Name, "/"),
		ImageID: c.Image, Node: p.Node, LogDriver: c.LogDriver, StartedAt: c.State.StartedAt.UTC(),
		FinishedAt: c.State.FinishedAt.UTC(), ExitCode: c.State.ExitCode, OOMKilled: c.State.OOMKilled,
	}, nil
}

func (p Policy) removable(s Snapshot, now time.Time) bool {
	end := s.FinishedAt
	if s.CompletedAt.After(end) {
		end = s.CompletedAt
	}
	keep := p.SuccessKeep
	if s.Status == "failed" || s.Status=="cancelled" {
		keep = p.FailureKeep
	}
	return keep > 0 && !now.Before(end.Add(keep))
}
