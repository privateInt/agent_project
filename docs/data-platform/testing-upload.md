# 파일 업로드 적용·검증

2026-09-11 사용자 서버에서 아래 절차를 통과했다. 이미 실제 업로드까지 PASS한 환경은 이번 완료 문서 적용을 위해 재빌드하거나 smoke를 반복할 필요가 없다. 아래 명령은 신규 환경 적용과 이후 변경 검증용이다.

기존 단일 URL 수집과 멱등 처리 수정본을 적용한 환경에 추가한다. 파일 1개의 동기 업로드이며 GPU를 사용하지 않는다. 실행 이미지 교체 대상은 **data-collection, data-gateway** 두 개다. 설정 생성, 호스트 Go 설치, venv, 추가 pip 설치는 필요 없다.

## 1. 패치와 Python 도구 확인

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
cd ~/workspace/agent_project/data_platform

python tests/test_upload_tools.py -v
python tests/test_collection_tools.py -v
```

새 테스트 7개는 smoke가 손상된 원본·다른 문서·잘못된 출처를 성공으로 처리하지 않는지 확인한다. 기존 8개는 수집 지원 도구 회귀다. 실제 Go 구현이나 MinIO 검증을 대체하지 않는다.

## 2. Go·PostgreSQL 테스트

테스트가 사용하는 DB는 기존 인프라 서버 안의 임시 schema다. 실제 수집 원본과 업무 테이블을 초기화하지 않는다. 아래 테스트에 새 형식 검증, multipart→gRPC 스트리밍, 동시 업로드·재전송·스토리지 실패·DB rollback·기존 스키마 확장·다운로드를 포함했다.

```bash
cd ~/workspace/agent_project/data_platform
(
  set -euo pipefail

  docker build --target builder -t data-platform-check:source-upload .

  DATA_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services["data-collection"].environment.POSTGRES_URL'
  )
  DATA_TEST_NETWORK=$(
    docker compose config --format json |
      jq -er '.networks["data-infra"].name'
  )
  export DATA_TEST_POSTGRES_URL

  mkdir -p tests/results/uploads
  docker run --rm --network "$DATA_TEST_NETWORK" \
    -e DATA_TEST_POSTGRES_URL \
    data-platform-check:source-upload \
    go test -count=1 -v \
      ./internal/sourcefile ./internal/blob ./internal/idempotency \
      ./collection/... ./gateway/... \
    2>&1 | tee tests/results/uploads/go-tests.log

  if grep -q -- '--- SKIP:' tests/results/uploads/go-tests.log; then
    echo 'DB tests skipped; check DATA_TEST_POSTGRES_URL.'
    exit 1
  fi
)
```

기존 수집 단계의 Go PASS와 별도로 이 변경분을 검증한다. FAIL 또는 SKIP이면 아래 이미지 전환을 진행하지 않는다.

## 3. Collection → Gateway 이미지 전환

```bash
cd ~/workspace/agent_project/data_platform
(
  set -euo pipefail

  docker compose build data-collection data-gateway
  docker compose up -d --no-build --no-deps --force-recreate data-collection
  docker compose up -d --no-build --no-deps --force-recreate data-gateway

  docker compose ps data-collection data-gateway
  docker compose logs --tail=40 data-collection data-gateway
)
```

Collection 시작 시 원본 테이블에 출처·원래 파일명·형식 필드를 추가한다. 기존 문서 ID·객체 키·해시는 유지하고 기존 행은 `source_type=crawl`로 읽는다. 자동 스키마 확장이 실패하면 Collection이 정상 기동하지 않으므로 로그를 확인한다. 업로드 행을 만든 뒤에는 이전 Collection 이미지로 되돌리지 않는다.

## 4. 실제 업로드 smoke

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_upload_smoke.py
```

기본 실행은 작은 TXT·DOCX·XLSX 3개를 만든다. 실제 MinIO 저장·읽기 검증, 동일 요청 재전송, 변경 요청 409, 문서 목록·상세·원본 다운로드, 해당 문서의 Operation이 없음을 확인한다. 성공한 원본 3개는 이후 정제 테스트에도 사용할 수 있도록 남긴다.

직전 수집 검증 문서까지 함께 확인하려면 다음 명령을 **기본 명령 대신** 사용한다.

```bash
python tests/live_upload_smoke.py \
  --existing-document-id a26b54fc-a381-4041-b48e-1328e7f5ebcf
```

이 ID는 2026-09-11 첫 수집 PASS 로그의 원본이다. 해당 데이터를 지웠거나 다른 환경이면 기본 명령을 사용한다. 선택한 기존 문서는 조회·다운로드만 수행한다.

보유한 실제 파일은 `--file`을 반복해 지정한다. 이 경우 기본 fixture 대신 지정 파일을 등록한다.

```bash
python tests/live_upload_smoke.py \
  --file /absolute/path/document.docx \
  --file /absolute/path/table.xlsx
```

`[record]` 보고서와 최종 PASS를 확인한다. 파일 형식 검사는 [업로드 API의 검사 범위](api-upload.md)에 한정하며, 문서 텍스트·표 추출 품질이나 OCR 품질 검증은 아직 아니다.

## 5. 응답 유실과 재실행

보고서는 첫 POST 전에 모든 파일의 키·SHA-256·크기를 기록한다. 네트워크 오류 등으로 응답이 유실됐으면 같은 보고서를 사용한다.

```bash
python tests/live_upload_smoke.py \
  --resume tests/results/uploads/실제-record-id.json
```

사용자 파일을 지정했던 경우 그 파일이 원래 위치에 있어야 하며, 바뀌었다면 어떤 업로드도 보내기 전에 중단한다. 기본 fixture는 기록된 종류로 재생성한다. 자동 업로드 재시도는 하지 않는다.

```bash
docker compose logs --tail=80 data-collection data-gateway
curl -fsS 'http://127.0.0.1:14000/api/source-documents?limit=10' | jq
```

## 현재 검증 상태

- Python: 새 7개 + 기존 8개, 총 15개 로컬·사용자 서버 PASS.
- Go·PostgreSQL: 새 테스트 11개를 포함한 22개·하위 사례 62개 PASS. FAIL·SKIP 없음. 동시 업로드·저장/DB 실패·스키마 확장·기존 수집 회귀 포함.
- 실제 MinIO·REST: TXT 71바이트, DOCX 797바이트, XLSX 1373바이트 업로드·동일 요청 재전송·변경 요청 409·원본 다운로드 PASS.
- 기존 크롤링 원본 `a26b54fc-a381-4041-b48e-1328e7f5ebcf` 조회·다운로드·해시 확인 PASS.
- 문서별 Operation 없음 확인. GPU와 새 수집 Job 없이 업로드하는 경로를 검증했다.

[실행 보고서 경로와 등록된 문서 ID](../history/data-platform-202609.md#2026-09-11-05-source-upload-verified). 실제 파일 smoke는 위 3개 작은 fixture를 대상으로 했다. PDF·이미지·HTML을 포함한 다양한 실문서의 파싱·OCR 품질은 이후 정제 단계에서 검증한다.
