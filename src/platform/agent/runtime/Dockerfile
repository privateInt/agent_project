# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e AS builder
WORKDIR /build
COPY runtime/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY contracts/ ./contracts/
RUN mkdir generated && python -m grpc_tools.protoc -Icontracts --python_out=generated --grpc_python_out=generated contracts/agent/platform/v1/platform.proto
COPY runtime/agent_runtime/ ./agent_runtime/
COPY runtime/tests/ ./tests/
FROM builder AS tests
RUN PYTHONPATH=/build/generated:/build python -m unittest discover -s tests -v
FROM builder AS runtime
WORKDIR /app
RUN cp -r /build/generated /app/generated && cp -r /build/agent_runtime /app/agent_runtime
ENV PYTHONPATH=/app/generated:/app PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
CMD ["python", "-m", "agent_runtime.server"]
