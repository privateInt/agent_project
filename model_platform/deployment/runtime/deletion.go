package runtime

import (
    "context"
    "fmt"
    "time"
)

// Inspect validates the owner labels, immutable plan, image, network and mount.
// Never use force or remove volumes/images: an externally restarted runtime
// causes docker rm to fail instead of terminating a live model.
func (d Docker) RemoveStopped(ctx context.Context, p Plan, expectedID string) error {
    e,err:=d.Inspect(ctx,p);if err!=nil { return err };if e==nil { return nil }
    if err=removableRuntime(e,expectedID);err!=nil { return err }
    if _,err=run(ctx,30*time.Second,"rm",e.ID);err!=nil { return err }
    again,err:=d.Inspect(ctx,p);if err!=nil { return err }
    if again!=nil { return fmt.Errorf("runtime removal is not confirmed") }
    return nil
}
func removableRuntime(e *Evidence,expectedID string) error {
    if !containerID.MatchString(expectedID) || e.ID!=expectedID { return fmt.Errorf("runtime identity changed") }
    if e.State.Running || e.State.Paused || e.State.Restarting || (e.State.Status!="exited" && e.State.Status!="created") { return fmt.Errorf("runtime is not stopped") }
    return nil
}
