// Explicit -f keeps this build-only graph separate from Compose runtime services.
group "default" {
  targets = ["management-import-job", "training-job", "deployment-job", "deployment-serving-job"]
}
group "jobs" {
  targets = ["management-import-job", "training-job", "deployment-job", "deployment-serving-job"]
}
target "go-build" {
  context = "."
  dockerfile = "Dockerfile"
}
target "management-import-job" {
  inherits = ["go-build"]
  target = "management-import-job"
  tags = ["model-platform-management-import-job:launch-v1"]
}
target "deployment-job" {
  inherits = ["go-build"]
  target = "deployment-job"
  tags = ["model-platform-deployment-job:launch-v1"]
}
target "training-binary" {
  inherits = ["go-build"]
  target = "training-binary"
}
target "training-job" {
  context = "."
  dockerfile = "jobs/training/axolotl/Dockerfile"
  target = "training-job"
  contexts = { training_binary = "target:training-binary" }
  tags = ["model-platform-training-axolotl:launch-v1"]
}
target "check" {
  inherits = ["go-build"]
  target = "builder"
  tags = ["model-platform-check:build-layout"]
}
target "docker-runtime" {
  inherits = ["go-build"]
  target = "runtime-docker"
}
target "dispatch-fault" {
  context = "."
  dockerfile = "tests/dispatch_fault/Dockerfile"
  contexts = {
    go_builder = "target:check"
    docker_runtime = "target:docker-runtime"
  }
  tags = ["model-platform-dispatch-fault:20260908"]
}

// The serving coordinator uses a distinct tag to reject old demo-only images.
target "deployment-serving-job" {
  inherits = ["go-build"]
  target = "deployment-job"
  tags = ["model-platform-deployment-job:serving-v1"]
}
