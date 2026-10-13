package gpuadmission

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

// No Config.Env, credentials, or arbitrary command arguments enter the inventory.
const inventoryFormat = `{"ID":{{json .Id}},"Name":{{json .Name}},"State":{{json .State}},"Config":{"Labels":{{json .Config.Labels}}},"HostConfig":{"Runtime":{{json .HostConfig.Runtime}},"DeviceRequests":{{json .HostConfig.DeviceRequests}}}}`

func docker(ctx context.Context, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	data, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	if err != nil {
		return data, fmt.Errorf("Docker GPU inventory command %s failed", args[0])
	}
	return data, nil
}
func DockerInventory(ctx context.Context) ([]Container, error) {
	data, err := docker(ctx, "ps", "-aq", "--no-trunc")
	if err != nil {
		return nil, err
	}
	ids := strings.Fields(string(data))
	if len(ids) == 0 {
		return nil, nil
	}
	// One inspect call for the daemon snapshot, not one subprocess per container.
	// A concurrent removal makes this observation unavailable and retries later.
	args := append([]string{"inspect", "--type", "container", "--format", inventoryFormat}, ids...)
	raw, err := docker(ctx, args...)
	if err != nil {
		return nil, err
	}
	var out []Container
	for _, line := range strings.Split(strings.TrimSpace(string(raw)), "\n") {
		var c Container
		if err := json.Unmarshal([]byte(line), &c); err != nil || c.ID == "" || c.Name == "" {
			return nil, fmt.Errorf("invalid Docker GPU inventory")
		}
		out = append(out, c)
	}
	if len(out) != len(ids) {
		return nil, fmt.Errorf("incomplete Docker GPU inventory")
	}
	return out, nil
}
func FromEnvironment() Guard {
	return Guard{Dir: os.Getenv("GPU_ADMISSION_DIR"), Inventory: DockerInventory}
}

// Start re-admits a previously created container, covering a stop/start race.
func Start(ctx context.Context, id string, action func() error) error {
	items, err := DockerInventory(ctx)
	if err != nil {
		return err
	}
	for _, c := range items {
		if c.ID != id {
			continue
		}
		gpu, ids := selectors(c)
		if !gpu {
			return fmt.Errorf("serving container has no GPU device request")
		}
		count := int32(len(ids))
		if count == 0 {
			count = 1
		}
		r := Request{Name: strings.TrimPrefix(c.Name, "/"), OperationID: c.Config.Labels["model-platform.operation_id"], GPUIds: SortedIDs(ids), GPUCount: count, Shared: c.Config.Labels["model-platform.owner"] == "deployment-serving"}
		return FromEnvironment().Run(ctx, r, action)
	}
	return fmt.Errorf("serving container disappeared before GPU admission")
}
