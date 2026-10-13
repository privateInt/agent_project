# 같은 호스트 링크·첨부 수집 적용·검증

2026-09-15. 단일 URL 수집을 확장해 한 CPU Job에서 여러 원본을 수집한다. 이후 사용자 서버에서 수집 Operation 완료, W3C HTML·DOCX·PDF 3개/첨부 2개, 전체 원본 해시·CPU Job 종료·조회 검사를 확인했다. 이 확인을 모든 사이트의 링크 수집 품질로 일반화하지 않는다. 기존 OCR 배포/정제는 이 패치의 변경 대상이 아니다.

## 이번 범위

- `follow_links`: seed와 같은 호스트의 HTML 페이지 링크를 너비 우선으로 탐색한다.
- `include_attachments`: HTML의 첨부 링크를 별도 원본 문서로 등록한다.
- 원본마다 `document_id`, 요청/최종 URL, 부모 URL, 깊이, 역할, 해시와 실제 Job 이미지 ID를 보존한다.
- 모든 선택 원본의 저장·무결성 확인 후 DB 트랜잭션 하나로 결과를 등록한다. 중간 실패는 성공이나 일부 완료로 표시하지 않는다.
- 이전 옵션 없는 요청, 기존 멱등 키, 기존 원본 ID/객체 키와 `output_document_id`를 유지한다. 다중 결과는 `output_document_ids`로 조회한다.

이 단계는 HTTP 응답에 이미 들어 있는 `<a href>` 링크를 수집한다. JavaScript 실행, 로그인, 쿠키·헤더 입력, 클릭/폼 제출, iframe·CSS·이미지 리소스 탐색은 포함하지 않는다. JavaScript가 필요한 경우 구현된 [브라우저 수집](testing-browser-collection.md)을 사용한다.

## 1. 패치 적용·Go/DB 검사·서비스 교체

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform

  python -m unittest discover -s tests -p 'test_collection_tools.py' -v
  python -m unittest discover -s tests -p 'test_upload_tools.py' -v

  docker build --target builder -t data-platform-check:collection-scope .
  DATA_SCOPE_TEST_POSTGRES_URL=$(docker compose config --format json |
    jq -er '.services["data-operation"].environment.POSTGRES_URL')
  DATA_SCOPE_TEST_NETWORK=$(docker compose config --format json |
    jq -er '.networks["data-infra"].name')
  export DATA_TEST_POSTGRES_URL="$DATA_SCOPE_TEST_POSTGRES_URL"
  mkdir -p tests/results/collection-scope
  docker run --rm --network "$DATA_SCOPE_TEST_NETWORK" \
    -e DATA_TEST_POSTGRES_URL data-platform-check:collection-scope \
    go test -count=1 -v ./... \
    2>&1 | tee tests/results/collection-scope/server-go-tests.log
  if grep -q -- '--- SKIP:' tests/results/collection-scope/server-go-tests.log; then
    echo 'DB tests were skipped; check DATA_TEST_POSTGRES_URL.'
    exit 1
  fi

  docker compose --profile jobs build \
    data-collection data-gateway data-reconciler crawl-job

  if test -n "$(docker ps -q --filter label=data-platform.owner=dispatcher)"; then
    echo 'A Data Job is running. Wait for completion, then run the service-update command in the guide.'
    exit 1
  fi
  docker compose up -d --no-deps data-collection data-gateway data-reconciler
)
```

실행 중인 Data Job 때문에 마지막 서비스 교체만 중단됐다면 빌드/검사를 반복하지 않는다. Job 종료 뒤 아래 명령으로 교체한다.

```bash
cd ~/workspace/agent_project/data_platform
docker compose up -d --no-deps data-collection data-gateway data-reconciler
```

DB 테스트는 무작위 이름의 별도 schema에서 수행하고 제거한다. 업무 테이블을 초기화하지 않는다. 새 Collection 시작 시 기존 `crawl_run_id` 단일 문서 제약을 URL별 문서 제약으로 교체하고 결과 목록·출처 컬럼을 추가한다. 기존 원본의 ID·객체 키·해시는 유지한다. 다중 원본이 등록된 뒤에는 이전 Collection 이미지로 되돌리는 롤백을 지원하지 않는다.

## 2. 같은 호스트 3페이지 실제 수집

외부 테스트 서비스의 [HTML 링크 엔드포인트](https://httpbin.org/legacy)를 사용한다. 외부 서비스 응답 상태에 영향을 받으므로 실제 성공은 실행 로그로 판정한다. 이 검사는 링크 탐색과 다중 원본 등록이며 첨부파일 다운로드 검사는 아니다.

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_collection_smoke.py \
  --url 'https://httpbin.org/links/3/0' \
  --follow-links --max-depth 1 --max-documents 3 \
  --expect-min-documents 3
```

성공 시 `[scope]`에 `documents: 3`, 마지막에 `all 3 original hashes`가 출력된다. 보고서는 `tests/results/collection/<키>.json`이다. 모든 문서의 크기/해시, 목록 조회, 동일 키 재전송/변경 요청 충돌, Operation 1개, CPU Job 1개 종료와 이미지 일치를 확인한다. 목록/다운로드는 추가 Job을 만들지 않는다.

## 3. 첨부 링크가 있는 실제 대상 페이지

수집할 공개 페이지 URL을 입력한다. 아래 명령의 빈 값 그대로 실행하면 요청 전에 중단된다. seed와 **동일한 호스트**의 PDF·DOCX·XLSX·TXT·Markdown·PNG/JPEG 링크 또는 `download` 속성이 붙은 링크가 대상이다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform
  read -r -p '첨부 링크가 있는 공개 페이지 URL: ' DATA_SCOPE_URL
  test -n "$DATA_SCOPE_URL"
  python tests/live_collection_smoke.py \
    --url "$DATA_SCOPE_URL" \
    --follow-links --include-attachments \
    --max-depth 1 --max-documents 5 \
    --expect-min-documents 2 --expect-attachments 1
)
```

`--expect-attachments 1` 때문에 첨부가 하나도 없으면 PASS가 되지 않는다. URL에 확장자가 없고 `download` 속성도 없는 다운로드 링크는 `follow_links`로 선택한 후 응답 Content-Type이 HTML이 아니면 첨부로 기록한다. 첨부 옵션이 꺼져 있으면 이 응답은 실패로 처리한다. 링크 판정은 파일 형식 검증이나 문서 정제가 아니다. 수집 후 정제는 기존 `document_id` 기반 독립 API로 요청한다. `application/octet-stream`처럼 형식이 명확하지 않은 원본은 기존 정제 계약에서 거절될 수 있다.

## 재개·실패 처리

타임아웃이나 응답 유실 때 새 명령을 처음부터 실행하지 말고 출력된 실제 보고서로 재개한다.

```bash
python tests/live_collection_smoke.py \
  --resume tests/results/collection/실제-보고서-키.json
```

재개 시 URL·옵션·기대 개수와 멱등 키는 보고서에서 가져온다. 이미 `failed`인 Operation을 같은 키로 다시 실행하지 않는다. 다운로드는 자동 재시도하지 않으며 정확히 같은 결과 등록만 제한적으로 재전송한다. 등록 응답이 불확실하면 보존된 객체와 Operation을 조회해 확인한다. Reconciler는 모든 등록 문서를 확인한 후 완료 보고를 복구하며 크롤링을 재실행하지 않는다.

HTTP 403/429, TLS/공개 DNS, 외부 리다이렉트 등의 실패는 실제 수집 실패로 남긴다. 중간 저장 객체는 남을 수 있지만 원본 문서가 부분 등록되지는 않는다. 객체 정리는 이후 GC 범위다.

## 범위·한도

| 옵션/정책 | 기본값과 한도 |
|---|---|
| `follow_links` / `include_attachments` | 둘 다 false. 이때 기존 단일 URL 동작 |
| `max_bytes` | 문서당 8 MiB, 최대 32 MiB |
| `timeout_seconds` | 확장 수집의 다운로드·업로드 전체 120초, 5~300초. 완료 등록/보고는 별도 제한 |
| `max_depth` | follow_links일 때 기본 1, 최대 3. seed 깊이는 0 |
| `max_documents` | 확장 기본 10, 최대 20. seed·첨부 포함 |
| `max_total_bytes` | `min(max_bytes × max_documents, 64 MiB)`, `max_bytes` 이상 64 MiB 이하 |
| 페이지당 링크 해석 | `<a href>` 최대 256개. 토큰 버퍼 최대 1 MiB |
| 요청 간격 | 순차 요청, 원본 간 최소 250ms 대기. 자동 GET 재시도 없음 |
| 네트워크 | HTTP 80 / HTTPS 443, 공개 IP, DNS 확인한 주소로 연결, proxy 미사용 |
| 호스트 | seed의 정확한 hostname. `www`·서브도메인·외부 파일 CDN도 다른 호스트 |
| 리다이렉트 | 확장은 seed부터 모든 단계에 같은 호스트 적용. HTTPS→HTTP 금지 |
| 첨부 깊이 | 허용한 최종 깊이의 페이지에서도 직접 연결된 첨부 수집 가능(페이지 깊이+1) |

확장 모드가 꺼져 있는데 탐색 한도를 보내면 거절한다. 단일 URL의 기존 공개 호스트 간 리다이렉트 정책은 유지한다. fragment를 제거한 정규화 요청 URL은 중복 예약하지 않는다. query는 보존하며 서로 다른 요청 URL이 같은 최종 URL/바이트를 반환한 경우 출처별로 별도 문서가 될 수 있다. HTTP 원본 바이트는 그대로 저장하고 charset 변환은 링크 분석에만 사용한다. 구현 참고: [Go HTML tokenizer](https://pkg.go.dev/golang.org/x/net/html), [charset reader](https://pkg.go.dev/golang.org/x/net/html/charset).

개수·깊이·페이지 링크 해석 상한으로 생략된 후보는 각각 `document_limit_reached`, `depth_limit_reached`, `link_limit_reached`로 표시한다. 상한 내 선택 원본 등록은 `succeeded`일 수 있으나 전체 사이트 수집 완료를 뜻하지 않는다. 용량·시간 초과나 선택 URL의 실패는 전체 실행 실패다. 첨부 분류에 사용한 허용 확장자 외의 경로는 `download` 속성이 없으면 탐색하지 않는다.

## 검증 상태

최초 로컬 Go/DB SKIP 이후 서버 Go/DB 110개·하위106개, Python19개와 실제 HTML 3페이지·W3C HTML/DOCX/PDF 수집을 확인했다. [현재 상태](status.md), [수집 검증 이력](../history/data-platform-202609.md#2026-09-15-08-collection-scope-verified)을 따른다. 브라우저 수집과 Streamlit도 구현·서버 검사를 완료했으며 대표 운영 문서 품질과 Data 운영 마무리는 별도다.
