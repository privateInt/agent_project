# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --chmod=0644 console/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home console \
    && install -d -m 0755 /app/.streamlit \
    && install -d -m 0700 -o 10001 -g 10001 /var/lib/data-console
COPY --chmod=0644 console/app.py console/data_api.py console/data_state.py console/data_ui.py console/data_ui_actions.py console/data_ui_documents.py console/data_preview.py console/data_requests.py console/data_results.py console/check_connection.py /app/
COPY --chmod=0644 console/data_style.py console/data_ui_deletion.py console/data_ui_groups.py /app/
COPY --chmod=0644 console/.streamlit/config.toml /app/.streamlit/config.toml
USER console
RUN python -c "import os, tomllib; from pathlib import Path; import data_api, data_state, data_preview, data_requests, pypdfium2, PIL; assert os.getuid()==10001; probe=Path('/var/lib/data-console/.write-check'); probe.write_bytes(b''); probe.unlink(); [compile(p.read_text(), str(p), 'exec') for p in Path('.').glob('*.py')]; tomllib.loads(Path('.streamlit/config.toml').read_text())"
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"
CMD ["streamlit", "run", "app.py"]

FROM runtime AS tests
# Use the existing, runtime-verified /app directory. Do not depend on implicit
# COPY parent-directory permissions or discovery through a new tests directory.
COPY --chmod=0644 tests/test_data_console.py /app/test_data_console.py
COPY --chmod=0644 tests/test_data_preview.py /app/test_data_preview.py
COPY --chmod=0644 tests/test_data_requests.py /app/test_data_requests.py
COPY --chmod=0644 tests/test_data_connection.py /app/test_data_connection.py
ENV DATA_CONSOLE_SOURCE_DIR=/app
COPY --chmod=0644 tests/test_data_tables.py /app/test_data_tables.py
RUN python /app/test_data_tables.py -v
RUN python /app/test_data_requests.py -v
RUN python /app/test_data_preview.py -v
RUN python /app/test_data_console.py -v
RUN python /app/test_data_connection.py -v
CMD ["python", "/app/test_data_console.py", "-v"]
