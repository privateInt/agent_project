package execution

import (
	"fmt"
	"sort"
	"strings"

	commonv1 "model-platform/gen/common/v1"
)

// Resources is the runtime-neutral compute placement carried by Operation,
// Kafka, Dispatcher, and Executor.
type Resources struct {
	NodeName string  `json:"node_name,omitempty"`
	GPUCount int32   `json:"gpu_count,omitempty"`
	GPUIds   []int32 `json:"gpu_ids,omitempty"`
}

// NormalizeResources validates a request and applies a domain default GPU
// count only when the caller did not request any GPU explicitly.
func NormalizeResources(req *commonv1.ExecutionResources, defaultGPUCount int32) (Resources, error) {
	out := Resources{}
	if req != nil {
		out.NodeName = strings.TrimSpace(req.GetNodeName())
		out.GPUCount = req.GetGpuCount()
		out.GPUIds = append([]int32(nil), req.GetGpuIds()...)
	}
	if out.GPUCount < 0 {
		return Resources{}, fmt.Errorf("gpu_count must be >= 0")
	}
	seen := map[int32]struct{}{}
	for _, id := range out.GPUIds {
		if id < 0 {
			return Resources{}, fmt.Errorf("gpu_ids must be >= 0")
		}
		if _, exists := seen[id]; exists {
			return Resources{}, fmt.Errorf("gpu_ids contains duplicate id %d", id)
		}
		seen[id] = struct{}{}
	}
	if len(out.GPUIds) > 0 {
		if out.GPUCount != 0 && out.GPUCount != int32(len(out.GPUIds)) {
			return Resources{}, fmt.Errorf("gpu_count must equal len(gpu_ids) when exact gpu_ids are specified")
		}
		out.GPUCount = int32(len(out.GPUIds))
		sort.Slice(out.GPUIds, func(i, j int) bool { return out.GPUIds[i] < out.GPUIds[j] })
	} else if out.GPUCount == 0 && defaultGPUCount > 0 {
		out.GPUCount = defaultGPUCount
	}
	return out, nil
}

func ResourcesFromProto(req *commonv1.ExecutionResources) Resources {
	if req == nil {
		return Resources{}
	}
	return Resources{
		NodeName: strings.TrimSpace(req.GetNodeName()),
		GPUCount: req.GetGpuCount(),
		GPUIds:   append([]int32(nil), req.GetGpuIds()...),
	}
}

func (r Resources) Proto() *commonv1.ExecutionResources {
	return &commonv1.ExecutionResources{
		NodeName: r.NodeName,
		GpuCount: r.GPUCount,
		GpuIds:   append([]int32(nil), r.GPUIds...),
	}
}
