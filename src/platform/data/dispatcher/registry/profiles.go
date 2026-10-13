package registry

import (
	"data-platform/internal/config"
	"data-platform/internal/execution"
	"fmt"
)

type Registry struct{}

func (Registry) Get(kind string) (execution.Profile, error) {
	env := []string{"OPERATION_GRPC_ADDRESS", "COLLECTION_GRPC_ADDRESS", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_USE_SSL"}
	switch kind {
	case "collection.crawl":
		return execution.Profile{Image: config.Env("CRAWL_JOB_IMAGE", "data-platform-crawl-job:dev"), ForwardEnv: env}, nil
	case "collection.browser":
		return execution.Profile{Image: config.Env("BROWSER_JOB_IMAGE", "data-platform-browser-job:dev"), ForwardEnv: env}, nil
	case "refinement.parse":
		return execution.Profile{Image: config.Env("REFINEMENT_JOB_IMAGE", "data-platform-refinement-job:dev"), ForwardEnv: append(env, "REFINEMENT_GRPC_ADDRESS", "MODEL_DEPLOYMENT_GRPC_ADDRESS")}, nil
	default:
		return execution.Profile{}, fmt.Errorf("unsupported execution type")
	}
}
