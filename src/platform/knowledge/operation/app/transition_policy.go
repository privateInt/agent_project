package app

// Retry scheduling is not implemented here. Failed work cannot be revived.
func transitionPolicy(current, target string) (allowed, noop bool) {
 if current == "succeeded" || current == "failed" || current == "cancelled" {
  return current == target && target != "cancelled", current == target && target != "cancelled"
 }
 switch target {
 case "running":
  return current == "dispatching" || current == "running", current == "running"
 case "phase", "succeeded":
  return current == "dispatching" || current == "running", false
 case "failed":
  return current == "pending" || current == "dispatching" || current == "running" || current == "retry_wait", false
 default:
  return false, false
 }
}
