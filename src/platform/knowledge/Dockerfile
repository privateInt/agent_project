# syntax=docker/dockerfile:1
FROM golang:1.25-bookworm AS core-tests
WORKDIR /src
COPY internal/chunking/ ./internal/chunking/
RUN go mod init knowledge-platform && go test -count=1 ./internal/chunking

FROM golang:1.25-bookworm AS go-deps
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev ca-certificates && rm -rf /var/lib/apt/lists/*
RUN GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12 \
 && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
WORKDIR /src
# Same proven dependency pins as Model Platform. Module files live in builder.
RUN go mod init knowledge-platform \
 && go get github.com/google/uuid@v1.6.0 \
 && go get github.com/jackc/pgx/v5@v5.10.0 \
 && go get github.com/minio/minio-go/v7@v7.3.0 \
 && go get github.com/twmb/franz-go@v1.21.6 \
 && go get golang.org/x/net@v0.58.0 \
 && go get google.golang.org/grpc@v1.83.2 \
 && go get google.golang.org/protobuf@v1.36.12
FROM go-deps AS builder
COPY contracts/ ./contracts/
RUN protoc --proto_path=/src/contracts --proto_path=/usr/include \
 --go_out=/src --go_opt=module=knowledge-platform \
 --go-grpc_out=/src --go-grpc_opt=module=knowledge-platform \
 /src/contracts/common/v1/resources.proto /src/contracts/operation/v1/operation.proto \
 /src/contracts/data/collection/v1/collection.proto /src/contracts/data/refinement/v1/refinement.proto \
 /src/contracts/chunking/v1/chunking.proto /src/contracts/model/embedding/v1/embedding.proto /src/contracts/model/reranking/v1/reranking.proto /src/contracts/indexing/v1/indexing.proto
COPY internal/ ./internal/
COPY operation/ ./operation/
COPY service/ ./service/
COPY dispatcher/ ./dispatcher/
COPY reconciler/ ./reconciler/
COPY gateway/ ./gateway/
COPY jobs/ ./jobs/
COPY ops/verify_cleanup/ ./ops/verify_cleanup/
RUN go mod tidy
FROM builder AS tests
RUN --mount=type=cache,target=/root/.cache/go-build go test -count=1 ./...

FROM builder AS build-operation
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/operation ./operation

FROM builder AS build-service
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/service ./service

FROM builder AS build-gateway
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/gateway ./gateway

FROM builder AS build-dispatcher
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/dispatcher ./dispatcher

FROM builder AS build-reconciler
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/reconciler ./reconciler

FROM builder AS build-chunking-job
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/chunking-job ./jobs/chunking

FROM builder AS build-indexing-job
RUN --mount=type=cache,target=/root/.cache/go-build CGO_ENABLED=0 go build -trimpath -o /out/indexing-job ./jobs/indexing

FROM builder AS build-cleanup-verifier
RUN --mount=type=cache,target=/root/.cache/go-build go test -count=1 ./ops/verify_cleanup && CGO_ENABLED=0 go build -trimpath -o /out/verify-cleanup ./ops/verify_cleanup

FROM docker:cli AS docker-cli
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
FROM runtime AS docker-runtime
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

FROM runtime AS operation
COPY --from=build-operation /out/operation /app/operation
CMD ["/app/operation"]

FROM runtime AS service
COPY --from=build-service /out/service /app/service
CMD ["/app/service"]

FROM runtime AS gateway
COPY --from=build-gateway /out/gateway /app/gateway
CMD ["/app/gateway"]

FROM docker-runtime AS dispatcher
COPY --from=build-dispatcher /out/dispatcher /app/dispatcher
CMD ["/app/dispatcher"]

FROM docker-runtime AS reconciler
COPY --from=build-reconciler /out/reconciler /app/reconciler
CMD ["/app/reconciler"]

FROM runtime AS chunking-job
COPY --from=build-chunking-job /out/chunking-job /app/chunking-job
LABEL knowledge-platform.execution-gate="1"
USER 65532:65532
CMD ["/app/chunking-job"]

FROM runtime AS indexing-job
COPY --from=build-indexing-job /out/indexing-job /app/indexing-job
LABEL knowledge-platform.execution-gate="1"
USER 65532:65532
CMD ["/app/indexing-job"]

FROM runtime AS cleanup-verifier
COPY --from=build-cleanup-verifier /out/verify-cleanup /app/verify-cleanup
USER 65532:65532
ENTRYPOINT ["/app/verify-cleanup"]

FROM gateway AS default
