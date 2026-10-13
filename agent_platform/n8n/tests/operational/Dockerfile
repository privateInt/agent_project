# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
WORKDIR /checks
COPY agent_platform/n8n/ops/ ./agent_platform/n8n/ops/
COPY agent_platform/n8n/tests/operational_check.py ./agent_platform/n8n/tests/
COPY infra/n8n/ops/api_access.py ./infra/n8n/ops/
RUN chmod -R a+rX /checks
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65532:65532
ENTRYPOINT ["python3", "/checks/agent_platform/n8n/tests/operational_check.py"]
