#!/usr/bin/env bash
# Update PDF intake / parsing fallback and its result display. No data requests.
set -euo pipefail
PLATFORM_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PLATFORM_DIR"
mkdir -p tests/results/pdf-ocr-fallback

# Finish build/test gates before replacing either running service.
docker build --progress=plain --target builder -t data-platform-check:pdf-ocr . \
  2>&1 | tee tests/results/pdf-ocr-fallback/server-builder.log
docker run --rm --network none data-platform-check:pdf-ocr \
  go test -count=1 -v ./internal/sourcefile \
  2>&1 | tee tests/results/pdf-ocr-fallback/server-sourcefile.log
docker build --progress=plain --target parser-tests -t data-platform-check:pdf-ocr-parser . \
  2>&1 | tee tests/results/pdf-ocr-fallback/server-parser.log
docker build --progress=plain -f console/Dockerfile --target tests -t data-platform-console-tests:dev . \
  2>&1 | tee tests/results/pdf-ocr-fallback/server-ui.log

export DATA_UI_BIND_ADDRESS="${DATA_UI_BIND_ADDRESS:-0.0.0.0}"
export DATA_UI_OCR_DEPLOYMENT_ID="${DATA_UI_OCR_DEPLOYMENT_ID:-29aac028-c0ad-49f6-a3fa-c2b3b67feade}"
docker compose --profile jobs --profile ui build data-collection refinement-job data-console \
  2>&1 | tee tests/results/pdf-ocr-fallback/server-build.log
docker compose --profile ui up -d --no-deps data-collection data-console
printf '%s\n' '빌드·재생성 명령을 완료했습니다. 브라우저에서 입력 수정 → 원본 PDF 선택 → 가져오기 → 내용 정리를 진행하세요.'
