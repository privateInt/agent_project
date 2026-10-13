FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY *.py /app/ops_platform/
COPY web/ /app/ops_platform/web/
COPY profiles/ /app/ops_platform/profiles/
USER 65532:65532
# Fail the build if runtime imports or console assets are missing; no keys/network required.
RUN python3 -B -c "from ops_platform.server import create_app, WEB; assert all((WEB / name).is_file() for name in ('index.html', 'app.js', 'style.css'))"
CMD ["python3", "-B", "-m", "ops_platform.server"]
