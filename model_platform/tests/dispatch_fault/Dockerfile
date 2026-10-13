# syntax=docker/dockerfile:1
# All inputs come from the same Bake build graph; no previously tagged control image.
FROM go_builder AS helper-builder
RUN --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux go build -o /out/fault-driver ./tests/dispatch_fault \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/operation ./operation \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/dispatcher ./dispatcher \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/reconciler ./reconciler \
    && CGO_ENABLED=0 GOOS=linux go build -o /out/deployment ./deployment

# This bundle is only for isolated fault testing, never a normal Compose service.
FROM docker_runtime AS dispatch-fault
LABEL model-platform.dispatch-fault-test="20260908"
COPY --from=helper-builder /out/ /app/
COPY --from=helper-builder /out/fault-driver /fault-bin/docker
CMD ["/app/fault-driver"]
