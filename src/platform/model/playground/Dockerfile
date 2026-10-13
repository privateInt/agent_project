# syntax=docker/dockerfile:1
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY --chmod=0644 playground/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home playground \
    && install -d -m 0755 /app /app/.streamlit
# Host umask must not make the runtime source or Streamlit config unreadable.
COPY --chmod=0644 playground/*.py /app/
COPY --chmod=0644 playground/.streamlit/config.toml /app/.streamlit/config.toml
USER playground
# Import the entry point and its transitive view modules after dropping root.
# Importing app does not execute main() or issue Gateway requests.
RUN python -c "import os, tomllib; from pathlib import Path; import app; assert os.getuid() == 10001; [compile(p.read_text(), str(p), 'exec') for p in Path('.').glob('*.py')]; tomllib.loads(Path('.streamlit/config.toml').read_text())"
EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=3).read()"
CMD ["streamlit", "run", "app.py"]
