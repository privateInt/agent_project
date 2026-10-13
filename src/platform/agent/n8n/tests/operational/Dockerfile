# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
WORKDIR /checks
COPY src/platform/agent/n8n/ops/ ./src/platform/agent/n8n/ops/
COPY src/platform/agent/n8n/tests/operational_check.py ./src/platform/agent/n8n/tests/
COPY src/infra/n8n/ops/api_access.py ./src/infra/n8n/ops/
RUN chmod -R a+rX /checks
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python3", "/checks/src/platform/agent/n8n/tests/operational_check.py"]
