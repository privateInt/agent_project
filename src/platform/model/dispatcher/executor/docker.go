package executor

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"strconv"
	"strings"
 "regexp"
 "github.com/google/uuid"

	"model-platform/internal/execution"
	"model-platform/internal/gpuadmission"
)

type Docker struct {
	Network  string
	NodeName string
}

func NewDocker(network, nodeName string) *Docker {
	return &Docker{Network: network, NodeName: strings.TrimSpace(nodeName)}
}

// [FLOW-13] Dispatcher -> Docker process boundary.
// Generic placement is translated here:
//   - node_name => local dispatcher node validation
//   - gpu_ids   => docker --gpus device=0,1
//   - gpu_count => docker --gpus N
//
// A future KubernetesExecutor can translate the same execution.Resources into
// Kubernetes node placement and GPU/DRA requests without changing domain APIs.
func (d *Docker) Run(ctx context.Context, cmd execution.Command, profile execution.Profile) error {
	if requested := strings.TrimSpace(cmd.Resources.NodeName); requested != "" {
		if d.NodeName == "" {
			return fmt.Errorf("node_name=%q requested but DISPATCHER_NODE_NAME is not configured", requested)
		}
		if requested != d.NodeName {
			return fmt.Errorf("requested node %q does not match docker dispatcher node %q", requested, d.NodeName)
		}
	}

    if _,err:=uuid.Parse(cmd.DispatchToken);err!=nil {return fmt.Errorf("valid dispatch token required")}
    label,err:=exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.execution-gate"}}`,profile.Image).CombinedOutput()
    if err!=nil || strings.TrimSpace(string(label))!="1" {return fmt.Errorf("job image is missing execution-gate=1 contract: %s",profile.Image)}
    if cmd.ExecutionType=="deployment.serve" {
        contract,err:=exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.deployment-serving"}}`,profile.Image).CombinedOutput()
        if err!=nil || strings.TrimSpace(string(contract))!="1" {return fmt.Errorf("deployment serving job contract is missing")}
    }
    // A serving coordinator only authorizes the long-lived runtime. Attaching
    // GPUs here would compete with the actual vLLM container for the same IDs.
    runtimeResources := cmd.Resources
    if cmd.ExecutionType == "deployment.serve" { runtimeResources = execution.Resources{} }
    name := "model-op-" + cmd.OperationID + "-" + cmd.DispatchToken
	args := []string{
		"run", "-d", "--name", name, "--network", d.Network,
		"--label", "model-platform.owner=operation-dispatcher",
        "--label", "model-platform.dispatch_token=" + cmd.DispatchToken,
        "-e", "DISPATCH_TOKEN=" + cmd.DispatchToken,
		"--label", "model-platform.operation_id=" + cmd.OperationID,
		"--label", "model-platform.execution_type=" + cmd.ExecutionType,
		"--label", "model-platform.requested_node=" + cmd.Resources.NodeName,
        "--label", "model-platform.execution_node=" + d.NodeName,
		"--label", "model-platform.requested_gpu_ids=" + gpuIDsString(cmd.Resources.GPUIds),
		"-e", "OPERATION_ID=" + cmd.OperationID,
		"-e", "RESOURCE_ID=" + cmd.ResourceID,
		"-e", "RESOURCE_TYPE=" + cmd.ResourceType,
		"-e", "EXECUTION_TYPE=" + cmd.ExecutionType,
		"-e", "REQUESTED_NODE_NAME=" + cmd.Resources.NodeName,
		"-e", "REQUESTED_GPU_COUNT=" + strconv.Itoa(int(cmd.Resources.GPUCount)),
		"-e", "REQUESTED_GPU_IDS=" + gpuIDsString(cmd.Resources.GPUIds),
	}

	if len(runtimeResources.GPUIds) > 0 {
		args = append(args, "--gpus", `"device=`+gpuIDsString(runtimeResources.GPUIds)+`"`)
		args = append(args, "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility")
	} else if runtimeResources.GPUCount > 0 {
		args = append(args, "--gpus", strconv.Itoa(int(runtimeResources.GPUCount)))
		args = append(args, "-e", "NVIDIA_DRIVER_CAPABILITIES=compute,utility")
	}

	if cmd.ExecutionType == "training.run" {
        if volume:=os.Getenv("TRAINING_OUTPUT_VOLUME");volume!=""{
            if !regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]+$`).MatchString(volume){return fmt.Errorf("invalid training output volume")}
            preserved,e:=exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.training-output-preservation"}}`,profile.Image).CombinedOutput()
            if e!=nil||strings.TrimSpace(string(preserved))!="1"{return fmt.Errorf("training Job image must support output preservation; rebuild training-job")}
            checkpoint,e:=exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.training-checkpoints"}}`,profile.Image).CombinedOutput()
            if e!=nil||strings.TrimSpace(string(checkpoint))!="1"{return fmt.Errorf("training Job image must support checkpoints; rebuild training-job")}
            evaluation,e:=exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.training-evaluation"}}`,profile.Image).CombinedOutput()
            if e!=nil||strings.TrimSpace(string(evaluation))!="1"{return fmt.Errorf("training Job image must support evaluation; rebuild training-job")}
            args=append(args,"--mount","type=volume,src="+volume+",dst=/training-outputs","-e","TRAINING_OUTPUT_ROOT=/training-outputs","-e","EXECUTION_NODE_NAME="+d.NodeName)
        }
        // NCCL uses shared memory for local GPU communication.
        args = append(args, "--shm-size", "2g")
        if runtimeResources.GPUCount > 1 {
            multi, e := exec.CommandContext(ctx,"docker","image","inspect","--format",`{{index .Config.Labels "model-platform.training-ddp"}}`,profile.Image).CombinedOutput()
            if e != nil || strings.TrimSpace(string(multi)) != "1" { return fmt.Errorf("training Job image does not support DDP; build the new training-job image") }
        }
    }
	for _, key := range profile.ForwardEnv {
		if value, ok := os.LookupEnv(key); ok {
			args = append(args, "-e", key+"="+value)
		}
	}
	args = append(args, profile.Image)
	args = append(args, profile.Command...)
	launch := func() error {
	output, err := exec.CommandContext(ctx, "docker", args...).CombinedOutput()
	if err != nil {
		message := strings.TrimSpace(string(output))
		if message == "" {
			message = err.Error()
		}
		return fmt.Errorf("docker run: %s", message)
	}
	return nil
    }
    if runtimeResources.GPUCount > 0 {
        return gpuadmission.FromEnvironment().Run(ctx,gpuadmission.Request{
            Name:name,OperationID:cmd.OperationID,GPUIds:gpuadmission.SortedIDs(runtimeResources.GPUIds),GPUCount:runtimeResources.GPUCount,Fenced:true,
        },launch)
    }
    return launch()
}

func gpuIDsString(ids []int32) string {
	parts := make([]string, 0, len(ids))
	for _, id := range ids {
		parts = append(parts, strconv.Itoa(int(id)))
	}
	return strings.Join(parts, ",")
}
