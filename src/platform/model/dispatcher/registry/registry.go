package registry

import (
	"fmt"
	"os"

	"model-platform/internal/execution"
)

type Registry struct{ profiles map[string]execution.Profile }

func New() *Registry {
	// [FLOW-12] execution_type -> execution profile.
	// Domain credentials are intentionally absent from every profile.
	return &Registry{profiles: map[string]execution.Profile{
		"management.import-huggingface": {
			Image:      env("MANAGEMENT_IMPORT_JOB_IMAGE", "model-platform-management-import-job:launch-v1"),
			Command:    []string{"/app/model-import-huggingface"},
			ForwardEnv: []string{"OPERATION_GRPC_ADDRESS", "MANAGEMENT_GRPC_ADDRESS", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_USE_SSL"},
		},
		"training.run": {
			Image: env("TRAINING_JOB_IMAGE", "model-platform-training-axolotl:launch-v1"),
			ForwardEnv: []string{
				"OPERATION_GRPC_ADDRESS", "TRAINING_GRPC_ADDRESS", "MANAGEMENT_GRPC_ADDRESS",
				"MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET", "MINIO_USE_SSL",
				"MLFLOW_TRACKING_URI", "MLFLOW_PUBLIC_URI", "MLFLOW_EXPERIMENT_NAME",
                "TRAINING_TEST_FAIL_PUBLICATION_RUN_ID", "TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID", "TRAINING_TEST_CHECKPOINT_MODE",
				"MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING", "MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL",
				"MLFLOW_SYSTEM_METRICS_SAMPLES_BEFORE_LOGGING",
			},
		},
        "deployment.serve": {
            Image: env("DEPLOYMENT_SERVING_JOB_IMAGE", "model-platform-deployment-job:serving-v1"),
            Command: []string{"/app/deployment-apply"},
            ForwardEnv: []string{"OPERATION_GRPC_ADDRESS", "DEPLOYMENT_GRPC_ADDRESS"},
        },
		"deployment.apply": {
			Image:      env("DEPLOYMENT_JOB_IMAGE", "model-platform-deployment-job:launch-v1"),
			Command:    []string{"/app/deployment-apply"},
			ForwardEnv: []string{"OPERATION_GRPC_ADDRESS", "DEPLOYMENT_GRPC_ADDRESS"},
		},
	}}
}

func (r *Registry) Get(executionType string) (execution.Profile, error) {
	profile, ok := r.profiles[executionType]
	if !ok {
		return execution.Profile{}, fmt.Errorf("unknown execution_type: %s", executionType)
	}
	return profile, nil
}
func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
