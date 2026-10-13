// Package gpuadmission serializes create/start ownership on one Docker daemon.
// Serving attempts may share devices; exclusive Jobs still wait for GPU owners.
// All platform GPU creators must mount the SAME local named volume at Dir.
// Persistent intent precedes Docker create: a lost reply never frees GPUs.
package gpuadmission

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strings"
	"syscall"
	"time"
)

type Request struct {
	Name        string  `json:"name"`
	OperationID string  `json:"operation_id"`
	GPUIds      []int32 `json:"gpu_ids,omitempty"`
	GPUCount    int32   `json:"gpu_count"`
	// Only execution-gated Jobs may rotate an absent old dispatch token.
	Fenced bool `json:"fenced,omitempty"`
	// Serving attempts may share physical GPUs. Jobs still request exclusive admission.
	Shared bool `json:"shared,omitempty"`
}
type Claim struct {
	Request
	Seen bool `json:"seen"`
}
type State struct {
	Version             int              `json:"version"`
	Claims              map[string]Claim `json:"claims"`
	CancelledOperations map[string]bool  `json:"cancelled_operations,omitempty"`
}
type DeviceRequest struct {
	Driver       string
	Count        int
	DeviceIDs    []string
	Capabilities [][]string
}
type Container struct {
	ID, Name string
	State    struct {
		Status                      string
		Running, Paused, Restarting bool
	}
	Config     struct{ Labels map[string]string }
	HostConfig struct {
		Runtime        string
		DeviceRequests []DeviceRequest
	}
}
type BusyError struct{ Owner string }

func (e *BusyError) Error() string { return "GPU reserved by " + e.Owner }

type Guard struct {
	Dir       string
	Inventory func(context.Context) ([]Container, error)
}

var safeName = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,180}$`)

func validate(r Request) error {
	if !safeName.MatchString(r.Name) || !safeName.MatchString(r.OperationID) || r.GPUCount < 1 {
		return fmt.Errorf("invalid GPU admission identity/count")
	}
	seen := map[int32]bool{}
	for _, id := range r.GPUIds {
		if id < 0 || seen[id] {
			return fmt.Errorf("invalid exact GPU IDs")
		}
		seen[id] = true
	}
	if len(r.GPUIds) > 0 && len(r.GPUIds) != int(r.GPUCount) {
		return fmt.Errorf("GPU IDs/count mismatch")
	}
	return nil
}

// Empty exact IDs mean a count-only legacy request, which reserves ALL devices.
// This is deliberately conservative: Docker chooses the physical devices.
func overlap(a, b []int32) bool {
	if len(a) == 0 || len(b) == 0 {
		return true
	}
	for _, x := range a {
		for _, y := range b {
			if x == y {
				return true
			}
		}
	}
	return false
}
func occupied(c Container) bool {
	return c.State.Running || c.State.Paused || c.State.Restarting || c.State.Status != "exited"
}
func selectors(c Container) (bool, []int32) {
	var ids []int32
	gpu := false
	for _, d := range c.HostConfig.DeviceRequests {
		uses := d.Driver == "nvidia"
		for _, group := range d.Capabilities {
			for _, v := range group {
				if v == "gpu" {
					uses = true
				}
			}
		}
		if !uses {
			continue
		}
		gpu = true
		if len(d.DeviceIDs) == 0 {
			return true, nil
		}
		for _, v := range d.DeviceIDs {
			// UUID/MIG selections cannot safely be compared to host index selections.
			var id int32
			if _, err := fmt.Sscan(v, &id); err != nil || fmt.Sprint(id) != v || id < 0 {
				return true, nil
			}
			ids = append(ids, id)
		}
	}
	if c.HostConfig.Runtime == "nvidia" && !gpu {
		return true, nil
	}
	return gpu, ids
}

func reconcile(s *State, items []Container) map[string]Container {
	byName := map[string]Container{}
	for _, c := range items {
		byName[strings.TrimPrefix(c.Name, "/")] = c
	}
	for name, claim := range s.Claims {
		c, ok := byName[name]
		if ok && !occupied(c) || !ok && claim.Seen {
			delete(s.Claims, name)
			continue
		}
		if ok {
			claim.Seen = true
			s.Claims[name] = claim
		}
	}
	return byName
}

// Run holds the host lock across read/inspect/reserve/create. Existing created
// containers reserve GPUs from exclusive Jobs until stopped. Shared serving
// attempts may overlap them. No TTL releases uncertain intent.
func (g Guard) Run(ctx context.Context, r Request, action func() error) error {
	if err := validate(r); err != nil {
		return err
	}
	if g.Inventory == nil || g.Dir == "" {
		return fmt.Errorf("GPU admission volume is not configured")
	}
	return g.locked(ctx, func(s *State) error {
		if s.CancelledOperations[r.OperationID] {
			return &CancelledError{}
		}
		items, err := g.Inventory(ctx)
		if err != nil {
			return fmt.Errorf("GPU inventory unavailable: %w", err)
		}
		byName := reconcile(s, items)
		// Persist observations even when another claim makes this attempt wait.
		if err := g.save(s); err != nil {
			return err
		}
		for name, claim := range s.Claims {
			if name == r.Name {
				if claim.OperationID != r.OperationID || claim.GPUCount != r.GPUCount || !reflect.DeepEqual(claim.GPUIds, r.GPUIds) || claim.Fenced != r.Fenced {
					return fmt.Errorf("GPU reservation identity changed")
				}
				continue
			}
			if claim.OperationID == r.OperationID && claim.Fenced && r.Fenced {
				// The Operation service has fenced the previous token before redispatch.
				if _, exists := byName[name]; !exists {
					delete(s.Claims, name)
					continue
				}
			}
			if !r.Shared && overlap(claim.GPUIds, r.GPUIds) {
				return &BusyError{Owner: name}
			}
		}
		for name, c := range byName {
			if name == r.Name || !occupied(c) {
				continue
			}
			// The legacy serving coordinator performs control work only. New versions
			// no longer attach GPUs to this execution-gated coordinator container.
			if c.Config.Labels["model-platform.owner"] == "operation-dispatcher" && c.Config.Labels["model-platform.execution_type"] == "deployment.serve" {
				continue
			}
			gpu, ids := selectors(c)
			if !r.Shared && gpu && overlap(ids, r.GPUIds) {
				return &BusyError{Owner: name}
			}
		}
		claim := Claim{Request: r}
		if _, ok := byName[r.Name]; ok {
			claim.Seen = true
		}
		s.Claims[r.Name] = claim
		if err := g.save(s); err != nil {
			return err
		} // MUST be durable before invoking Docker.
		actionErr := action()
		// Observation failure retains the durable reservation, including after a
		// successful CLI return. A later admission cycle can reconcile it.
		if items, e := g.Inventory(ctx); e == nil {
			reconcile(s, items)
			if e = g.save(s); e != nil && actionErr == nil {
				return e
			}
		}
		return actionErr
	})
}

// ReleaseStopped is called only after the owner has durably revoked its intent
// to start this serving runtime. A delayed create may appear but cannot be started
// by that controller. A never-started container is removed without force under
// the admission lock; exited containers and their logs are retained.
func (g Guard) ReleaseStopped(ctx context.Context, name, operationID string, removeCreated func(context.Context, string) error) error {
	if !safeName.MatchString(name) || !safeName.MatchString(operationID) {
		return fmt.Errorf("invalid GPU release identity")
	}
	if g.Dir == "" || g.Inventory == nil {
		return fmt.Errorf("GPU admission volume is not configured")
	}
	return g.locked(ctx, func(s *State) error {
		if old, ok := s.Claims[name]; ok && old.OperationID != operationID {
			return fmt.Errorf("GPU reservation owner mismatch")
		}
		items, err := g.Inventory(ctx)
		if err != nil {
			return err
		}
		for _, c := range items {
			if strings.TrimPrefix(c.Name, "/") != name || !occupied(c) {
				continue
			}
			if c.State.Status != "created" || c.State.Running || c.State.Paused || c.State.Restarting || removeCreated == nil {
				return fmt.Errorf("GPU runtime has not stopped")
			}
			if err := removeCreated(ctx, c.ID); err != nil {
				return err
			}
			again, err := g.Inventory(ctx)
			if err != nil {
				return err
			}
			for _, current := range again {
				if strings.TrimPrefix(current.Name, "/") == name && occupied(current) {
					return fmt.Errorf("never-started runtime removal is unconfirmed")
				}
			}
		}
		delete(s.Claims, name)
		return g.save(s)
	})
}

func (g Guard) locked(ctx context.Context, action func(*State) error) error {
	if err := os.MkdirAll(g.Dir, 0700); err != nil {
		return err
	}
	f, err := os.OpenFile(filepath.Join(g.Dir, "admission.lock"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return err
	}
	defer f.Close()
	for {
		err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
		if err == nil {
			break
		}
		if err != syscall.EWOULDBLOCK && err != syscall.EAGAIN {
			return err
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(50 * time.Millisecond):
		}
	}
	defer syscall.Flock(int(f.Fd()), syscall.LOCK_UN)
	if err := ctx.Err(); err != nil {
		return err
	}
	s := State{Version: 1, Claims: map[string]Claim{}}
	data, err := os.ReadFile(filepath.Join(g.Dir, "claims.json"))
	if err == nil {
		if json.Unmarshal(data, &s) != nil || s.Version != 1 || s.Claims == nil {
			return fmt.Errorf("GPU reservation state is invalid; admission blocked")
		}
	} else if !os.IsNotExist(err) {
		return err
	}
	for name, c := range s.Claims {
		if name != c.Name || validate(c.Request) != nil {
			return fmt.Errorf("invalid saved GPU reservation; admission blocked")
		}
	}
	for id, cancelled := range s.CancelledOperations {
		if !safeName.MatchString(id) || !cancelled {
			return fmt.Errorf("invalid saved cancellation fence; admission blocked")
		}
	}
	return action(&s)
}
func (g Guard) save(s *State) error {
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(g.Dir, "claims-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	defer f.Close()
	if _, err = f.Write(data); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(f.Name(), filepath.Join(g.Dir, "claims.json")); err != nil {
		return err
	}
	dir, err := os.Open(g.Dir)
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func SortedIDs(ids []int32) []int32 {
	out := append([]int32(nil), ids...)
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}
