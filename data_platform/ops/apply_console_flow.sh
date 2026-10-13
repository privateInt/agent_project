#!/usr/bin/env bash
# Validate the console image, then replace only the Streamlit service.
set -euo pipefail
PLATFORM_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PLATFORM_DIR"
mkdir -p tests/results/console

docker build --progress=plain -f console/Dockerfile --target tests \
  -t data-platform-console-tests:dev . \
  2>&1 | tee tests/results/console/server-flow-tests.log

export DATA_UI_BIND_ADDRESS="${DATA_UI_BIND_ADDRESS:-0.0.0.0}"
export DATA_UI_OCR_DEPLOYMENT_ID="${DATA_UI_OCR_DEPLOYMENT_ID:-29aac028-c0ad-49f6-a3fa-c2b3b67feade}"
docker compose --profile ui build data-console
docker compose --profile ui up -d --no-deps data-console
printf '%s\n' '화면 빌드·재생성 명령을 완료했습니다. 데이터 관리 페이지를 새로고침하세요.'
