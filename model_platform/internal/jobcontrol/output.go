package jobcontrol

import (
	"context"
	"fmt"
	operationv1 "model-platform/gen/operation/v1"
)

// Publication requires a terminal Operation and absence of a running producer.
// No stop/delete action occurs here; a missing retained container is acceptable
// because a sealed receipt is independently required by the caller.
func (r *Runtime) OutputStopped(ctx context.Context, op *operationv1.Operation) error {
	return r.stoppedOutput(ctx, op, false)
}

// A cancelled producer may supply an earlier sealed checkpoint, never a completed output.
func (r *Runtime) CheckpointStopped(ctx context.Context, op *operationv1.Operation) error {
	return r.stoppedOutput(ctx, op, true)
}
func (r *Runtime) stoppedOutput(ctx context.Context, op *operationv1.Operation, allowCancelled bool) error {
	if (op.GetStatus() != "failed" && op.GetStatus() != "succeeded" && !(allowCancelled && op.GetStatus() == "cancelled")) || op.GetExecutionType() != "training.run" || op.GetResourceType() != "training_run" || op.GetDispatchProtocol() != 1 ||
		!uuidID.MatchString(op.GetOperationId()) || !uuidID.MatchString(op.GetDispatchToken()) || op.GetExecutionStartedAt() == nil ||
		(op.GetResources().GetNodeName() != "" && op.GetResources().GetNodeName() != r.Node) {
		return fmt.Errorf("terminal local training execution required")
	}
	c, found, e := r.inspect(ctx, "model-op-"+op.GetOperationId()+"-"+op.GetDispatchToken())
	if e != nil {
		return e
	}
	if !found {
		return nil
	}
	if e = validate(c, op, r.Node, false); e != nil {
		return e
	}
	if c.State.Status != "exited" || c.State.Running || c.State.Paused || c.State.Restarting {
		return fmt.Errorf("training producer has not stopped")
	}
	return nil
}

// PurgeStopped covers all retained generations, including cancellation before
// BeginExecution. It only observes Docker; it never cancels a user's work.
func (r *Runtime) PurgeStopped(ctx context.Context, op *operationv1.Operation) error {
	if (op.GetStatus() != "failed" && op.GetStatus() != "succeeded" && op.GetStatus() != "cancelled") || op.GetExecutionType() != "training.run" || op.GetResourceType() != "training_run" || !uuidID.MatchString(op.GetOperationId()) || (op.GetResources().GetNodeName() != "" && op.GetResources().GetNodeName() != r.Node) {
		return fmt.Errorf("terminal local training operation required")
	}
	if r.Guard.Inventory == nil {
		return fmt.Errorf("producer inventory unavailable")
	}
	items, e := r.Guard.Inventory(ctx)
	if e != nil {
		return e
	}
	for _, c := range items {
		if c.Config.Labels["model-platform.operation_id"] != op.GetOperationId() {
			continue
		}
		if c.Config.Labels["model-platform.owner"] != "operation-dispatcher" || c.Config.Labels["model-platform.execution_type"] != "training.run" || c.State.Status != "exited" || c.State.Running || c.State.Paused || c.State.Restarting {
			return fmt.Errorf("training producer stop unconfirmed")
		}
	}
	return nil
}
