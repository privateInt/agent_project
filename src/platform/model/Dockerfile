# syntax=docker/dockerfile:1
# Shared Go dependencies are prepared before application source is copied.
FROM golang:1.25-bookworm AS go-deps

ARG PROTOC_GEN_GO_VERSION=v1.36.12
ARG PROTOC_GEN_GO_GRPC_VERSION=v1.6.2
RUN apt-get update \
    && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@${PROTOC_GEN_GO_VERSION} \
    && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@${PROTOC_GEN_GO_GRPC_VERSION}
WORKDIR /src
# Keep module files in the build image, following this project's host-file policy.
# Module downloads remain in the image so the builder can run tests as a container.
RUN go mod init model-platform \
    && go get github.com/google/uuid@v1.6.0 \
    && go get github.com/gofiber/fiber/v3@v3.5.0 \
    && go get github.com/jackc/pgx/v5@v5.10.0 \
    && go get github.com/minio/minio-go/v7@v7.3.0 \
    && go get github.com/twmb/franz-go@v1.21.6 \
    && go get google.golang.org/grpc@v1.83.2 \
    && go get google.golang.org/protobuf@v1.36.12

FROM go-deps AS go-contracts
COPY contracts/ ./contracts/
RUN protoc \
      --proto_path=/src/contracts \
      --proto_path=/usr/include \
      --go_out=/src --go_opt=module=model-platform \
      --go-grpc_out=/src --go-grpc_opt=module=model-platform \
      /src/contracts/common/v1/resources.proto \
      /src/contracts/operation/v1/operation.proto \
      /src/contracts/management/v1/management.proto \
      /src/contracts/training/v1/training.proto \
      /src/contracts/deployment/v1/deployment.proto \
      /src/contracts/ocr/v1/ocr.proto \
      /src/contracts/embedding/v1/embedding.proto \
      /src/contracts/reranking/v1/reranking.proto


FROM go-contracts AS go-shared
COPY internal/ ./internal/

# Runtime bases are shared; only Docker-observing services receive the Docker CLI.
FROM docker:cli AS docker-cli
FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
FROM runtime AS runtime-docker
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# gateway: copy only this component and shared code into its build stage.
FROM go-shared AS build-gateway
COPY gateway/ ./gateway/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && go test -count=1 ./gateway/app ./internal/embedding ./internal/reranking \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/gateway ./gateway
FROM runtime AS gateway
COPY --from=build-gateway /out/gateway /app/gateway
CMD ["/app/gateway"]

# management: copy only this component and shared code into its build stage.
FROM go-shared AS build-management
COPY management/ ./management/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/management ./management
FROM runtime AS management
COPY --from=build-management /out/management /app/management
CMD ["/app/management"]

# operation: copy only this component and shared code into its build stage.
FROM go-shared AS build-operation
COPY operation/ ./operation/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/operation ./operation
FROM runtime AS operation
COPY --from=build-operation /out/operation /app/operation
CMD ["/app/operation"]

# training: copy only this component and shared code into its build stage.
FROM go-shared AS build-training
COPY training/ ./training/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/training ./training
FROM runtime-docker AS training
COPY --from=build-training /out/training /app/training
CMD ["/app/training"]

# deployment: copy only this component and shared code into its build stage.
FROM go-shared AS build-deployment
COPY deployment/ ./deployment/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && go test -count=1 ./deployment ./deployment/runtime ./deployment/app ./internal/embedding ./internal/reranking \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/deployment ./deployment
FROM runtime-docker AS deployment
COPY --from=build-deployment /out/deployment /app/deployment
CMD ["/app/deployment"]

# SSH is confined to the central remote execution clients. The remote worker
# itself is invoked through docker exec and opens no listening application port.
FROM runtime-docker AS runtime-remote
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/*

# dispatcher: copy only this component and shared code into its build stage.
FROM go-shared AS build-dispatcher
COPY dispatcher/ ./dispatcher/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/dispatcher ./dispatcher
FROM runtime-remote AS dispatcher
COPY --from=build-dispatcher /out/dispatcher /app/dispatcher
CMD ["/app/dispatcher"]

# reconciler: copy only this component and shared code into its build stage.
FROM go-shared AS build-reconciler
COPY reconciler/ ./reconciler/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/reconciler ./reconciler
FROM runtime-remote AS reconciler
COPY --from=build-reconciler /out/reconciler /app/reconciler
CMD ["/app/reconciler"]

# management-import-job: copy only this component and shared code into its build stage.
FROM go-shared AS build-management-import-job
COPY jobs/management/import_huggingface/ ./jobs/management/import_huggingface/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && go test -count=1 ./jobs/management/import_huggingface \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/model-import-huggingface ./jobs/management/import_huggingface
FROM runtime AS management-import-job
LABEL model-platform.execution-gate="1"
COPY --from=build-management-import-job /out/model-import-huggingface /app/model-import-huggingface
CMD ["/app/model-import-huggingface"]

# deployment-job: copy only this component and shared code into its build stage.
FROM go-shared AS build-deployment-job
COPY jobs/deployment/apply/ ./jobs/deployment/apply/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/deployment-apply ./jobs/deployment/apply
FROM runtime AS deployment-job
LABEL model-platform.execution-gate="1"
LABEL model-platform.deployment-serving="1"
COPY --from=build-deployment-job /out/deployment-apply /app/deployment-apply
CMD ["/app/deployment-apply"]

# The Axolotl Dockerfile receives this artifact through a named Bake context.
FROM go-shared AS build-training-binary
COPY jobs/training/axolotl/ ./jobs/training/axolotl/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/training-axolotl ./jobs/training/axolotl
FROM scratch AS training-binary
COPY --from=build-training-binary /out/training-axolotl /out/training-axolotl

# gc: archive and collect stopped Jobs using the shared Docker CLI runtime.
FROM go-shared AS build-gc
COPY gc/ ./gc/
RUN --mount=type=cache,target=/root/.cache/go-build \
    go mod tidy && CGO_ENABLED=0 GOOS=linux go build -o /out/gc ./gc
FROM runtime-docker AS gc
COPY --from=build-gc /out/gc /app/gc
CMD ["/app/gc"]

# Complete source plus generated contracts for go test ./... and test-only builds.
# Production service targets do not depend on this stage.
FROM go-shared AS builder
COPY gateway/ ./gateway/
COPY management/ ./management/
COPY operation/ ./operation/
COPY training/ ./training/
COPY deployment/ ./deployment/
COPY dispatcher/ ./dispatcher/
COPY reconciler/ ./reconciler/
COPY gc/ ./gc/
COPY jobs/ ./jobs/
COPY tests/ ./tests/
RUN go mod tidy

# Opt-in CPU GC integration tests need Go and the real Docker CLI.
FROM builder AS gc-test
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

# A plain docker build has a small, unambiguous default; Compose selects each target.
FROM gateway AS default
