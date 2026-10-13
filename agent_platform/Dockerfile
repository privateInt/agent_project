# syntax=docker/dockerfile:1
FROM golang:1.26-bookworm@sha256:a688600ca24f8a4d3ca77f95b0dd40704a9fc787c826660eb7ba0b641b8b175d AS dependencies
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev ca-certificates \
 && test -f /usr/include/google/protobuf/empty.proto \
 && test -f /usr/include/google/protobuf/timestamp.proto \
 && rm -rf /var/lib/apt/lists/*
RUN GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12 \
 && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
WORKDIR /src
# Match existing platforms: module files and generated code stay in the builder.
RUN go mod init agent-platform \
 && go get github.com/google/uuid@v1.6.0 \
 && go get github.com/jackc/pgx/v5@v5.10.0 \
 && go get google.golang.org/grpc@v1.83.2 \
 && go get google.golang.org/protobuf@v1.36.12 \
 && go get github.com/modelcontextprotocol/go-sdk@v1.8.0 \
 && go get go.yaml.in/yaml/v3@v3.0.4
FROM dependencies AS builder
COPY contracts/ ./contracts/
RUN protoc -I/src/contracts -I/usr/include --go_out=/src --go_opt=module=agent-platform --go-grpc_out=/src --go-grpc_opt=module=agent-platform /src/contracts/agent/platform/v1/platform.proto
COPY internal/ ./internal/
COPY service/ ./service/
COPY gateway/ ./gateway/
COPY mcp/ ./mcp/
COPY ops/healthcheck/ ./ops/healthcheck/
RUN go mod tidy
FROM builder AS tests
RUN go test -count=1 ./...
FROM builder AS binaries
RUN CGO_ENABLED=0 go build -trimpath -o /out/service ./service \
 && CGO_ENABLED=0 go build -trimpath -o /out/gateway ./gateway \
 && CGO_ENABLED=0 go build -trimpath -o /out/mcp ./mcp \
 && CGO_ENABLED=0 go build -trimpath -o /out/healthcheck ./ops/healthcheck
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251 AS base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=binaries /out/healthcheck /app/healthcheck
USER 65532:65532
FROM base AS service
COPY --from=binaries /out/service /app/service
CMD ["/app/service"]
FROM base AS mcp
COPY --from=binaries /out/mcp /app/mcp
CMD ["/app/mcp"]
FROM base AS gateway
COPY --from=binaries /out/gateway /app/gateway
CMD ["/app/gateway"]

FROM tests AS database-tests
ENTRYPOINT ["go", "test", "-count=1", "-v", "./internal/store", "./service/app", "-run", "TestDatabaseFoundation|TestToolsDatabase|TestToolsEndToEnd|TestOpenAPI|TestKnowledge|TestAgent"]

FROM gateway AS default
