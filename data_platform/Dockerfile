# syntax=docker/dockerfile:1
FROM golang:1.25-bookworm AS go-deps
RUN apt-get update && apt-get install -y --no-install-recommends protobuf-compiler libprotobuf-dev ca-certificates && rm -rf /var/lib/apt/lists/*
RUN GOBIN=/usr/local/bin go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.12 \
 && GOBIN=/usr/local/bin go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2
WORKDIR /src
# Same proven dependency pins as Model Platform. Module files live in builder.
RUN go mod init data-platform \
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
 --go_out=/src --go_opt=module=data-platform \
 --go-grpc_out=/src --go-grpc_opt=module=data-platform \
 --go_opt=Mocr/v1/ocr.proto=data-platform/gen/ocr/v1 --go-grpc_opt=Mocr/v1/ocr.proto=data-platform/gen/ocr/v1 \
 /src/contracts/common/v1/resources.proto /src/contracts/operation/v1/operation.proto /src/contracts/collection/v1/collection.proto /src/contracts/refinement/v1/refinement.proto /src/contracts/ocr/v1/ocr.proto
COPY internal/ ./internal/
COPY operation/ ./operation/
COPY collection/ ./collection/
COPY refinement/ ./refinement/
COPY dispatcher/ ./dispatcher/
COPY reconciler/ ./reconciler/
COPY gateway/ ./gateway/
COPY jobs/ ./jobs/
RUN go mod tidy

# Keep the complete builder for Go/DB tests. Each runtime compiles only its own
# executable; shared dependencies and compiler cache remain reusable.

FROM builder AS build-operation
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/operation ./operation

FROM builder AS build-collection
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/collection ./collection

FROM builder AS build-refinement
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/refinement ./refinement

FROM builder AS build-refinement-job
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/refinement-job ./jobs/refinement/parse

FROM builder AS build-gateway
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/gateway ./gateway

FROM builder AS build-dispatcher
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/dispatcher ./dispatcher

FROM builder AS build-reconciler
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/reconciler ./reconciler

FROM builder AS build-crawl-job
RUN --mount=type=cache,target=/root/.cache/go-build \
 CGO_ENABLED=0 go build -trimpath -o /out/crawl-job ./jobs/collection/crawl
FROM docker:cli AS docker-cli
FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
FROM runtime AS docker-runtime
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker

FROM runtime AS operation
COPY --from=build-operation /out/operation /app/operation
CMD ["/app/operation"]

FROM runtime AS collection
COPY --from=build-collection /out/collection /app/collection
CMD ["/app/collection"]

FROM runtime AS refinement
COPY --from=build-refinement /out/refinement /app/refinement
CMD ["/app/refinement"]

FROM runtime AS gateway
COPY --from=build-gateway /out/gateway /app/gateway
CMD ["/app/gateway"]

FROM docker-runtime AS dispatcher
COPY --from=build-dispatcher /out/dispatcher /app/dispatcher
CMD ["/app/dispatcher"]

FROM docker-runtime AS reconciler
COPY --from=build-reconciler /out/reconciler /app/reconciler
CMD ["/app/reconciler"]

FROM runtime AS crawl-job
COPY --from=build-crawl-job /out/crawl-job /app/crawl-job
LABEL data-platform.execution-gate="1"
USER 65532:65532
CMD ["/app/crawl-job"]

FROM python:3.12-slim-bookworm AS parser-base
WORKDIR /app
COPY jobs/refinement/parse/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chmod=0444 jobs/refinement/parse/parser.py jobs/refinement/parse/ocr.py jobs/refinement/parse/table_compare.py /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

FROM parser-base AS parser-tests
COPY tests/ /app/tests/
# Parser tests resolve the same relative path used by source checkouts.
COPY jobs/refinement/parse/parser.py jobs/refinement/parse/ocr.py jobs/refinement/parse/table_compare.py /app/jobs/refinement/parse/
# COPY preserves host modes, including 0600 files from restrictive umasks.
# The experiment runs with the host UID, so every copied helper/fixture must
# be readable and directories traversable by an arbitrary non-root user.
RUN chmod -R a+rX /app/tests /app/jobs
USER 65532:65532
RUN python -m unittest discover -s tests -p 'test_refinement_parser.py' -v \
 && python -m unittest discover -s tests -p 'test_refinement_ocr.py' -v \
 && python -m unittest discover -s tests -p 'test_refinement_geometry.py' -v \
 && python -m unittest discover -s tests -p 'test_table_comparison.py' -v \
 && python -m unittest discover -s tests -p 'test_document_quality_audit.py' -v \
 && python -m unittest discover -s tests -p 'test_document_regions.py' -v \
 && python -m unittest discover -s tests -p 'test_raster_regions.py' -v \
 && python -B tests/live_document_regions.py --help > /dev/null \
 && python -B tests/live_raster_regions.py --help > /dev/null

FROM parser-base AS refinement-job
COPY --chmod=0555 --from=build-refinement-job /out/refinement-job /app/refinement-job
LABEL data-platform.execution-gate="1"
USER 65532:65532
# Exercise the actual Go -> Python CLI path as the runtime user during build.
RUN ["/app/refinement-job", "--self-test"]
CMD ["/app/refinement-job"]

FROM python:3.12-slim-bookworm AS browser-base
WORKDIR /app
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir playwright==1.62.0 \
 && python -m playwright install --with-deps --only-shell chromium \
 && apt-get update && apt-get install -y --no-install-recommends libseccomp2 \
 && rm -rf /var/lib/apt/lists/* \
 && chmod -R a+rX /ms-playwright
COPY --chmod=0444 jobs/collection/browser/browser.py /app/browser.py
FROM browser-base AS browser-job
COPY --chmod=0555 --from=build-crawl-job /out/crawl-job /app/crawl-job
LABEL data-platform.execution-gate="1"
USER 65532:65532
RUN ["/app/crawl-job", "--browser-self-test"]
CMD ["/app/crawl-job"]

FROM builder AS job-cleanup-tests
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
RUN --mount=type=cache,target=/root/.cache/go-build \
 go test -count=1 -v ./reconciler/app -skip '^TestLive'

FROM gateway AS default
