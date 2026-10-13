# JavaScript 브라우저 수집 적용·검증

2026-09-15 구현 당시 로컬 Go 85개 PASS / PostgreSQL 의존 37개 SKIP, Python 도구 17개 PASS였으며 로컬 Chromium 다운로드는 시간 초과였다. 이후 사용자 서버에서 Chromium 자체 검사·동적 DOM/첨부 수집·최초 응답 보존을 확인했다. [검증 요약](../testing/results-summary-20260916.md)을 따른다. 아래는 필요한 변경 후 회귀 절차이며 현재 실행 대기가 아니다.

## 이번 동작

`render_javascript=true`이면 별도 `collection.browser` CPU Job이 HTML을 실행하고 현재 DOM을 UTF-8 HTML로 저장한다. DOM의 동적 `<a href>`에도 기존 링크·첨부 탐색 옵션과 같은 호스트/깊이/개수 한도를 적용한다. 첨부는 HTTP 응답 바이트 그대로 수집한다. seed는 HTML이어야 한다.

렌더링 문서의 `/content`는 DOM이다. 최초 HTML 응답은 다른 객체에 보존하며 `/http-response`로 별도 다운로드한다. 둘의 해시·크기를 함께 확인해야 등록할 수 있다. DOM을 서버가 보낸 원래 바이트로 표시하지 않는다. `browser_capture.representation=rendered_dom`으로 구별한다. 기존 HTTP·업로드의 `browser_capture`는 null이며 `/content` 의미는 그대로다. 정제 API에 브라우저 문서 ID를 넘기면 DOM을 입력으로 사용한다.

## 1. 다운로드·적용·검사·기동

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform

  python tests/test_browser_tools.py -v
  python tests/test_collection_tools.py -v

  docker build --target builder -t data-platform-check:browser .
  DATA_BROWSER_TEST_POSTGRES_URL=$(docker compose config --format json |
    jq -er '.services["data-operation"].environment.POSTGRES_URL')
  DATA_BROWSER_TEST_NETWORK=$(docker compose config --format json |
    jq -er '.networks["data-infra"].name')
  export DATA_TEST_POSTGRES_URL="$DATA_BROWSER_TEST_POSTGRES_URL"
  mkdir -p tests/results/browser
  docker run --rm --network "$DATA_BROWSER_TEST_NETWORK" \
    -e DATA_TEST_POSTGRES_URL data-platform-check:browser \
    go test -count=1 -v ./... \
    2>&1 | tee tests/results/browser/server-go-tests.log
  if grep -q -- '--- SKIP:' tests/results/browser/server-go-tests.log; then
    echo 'DB tests were skipped; check DATA_TEST_POSTGRES_URL.'
    exit 1
  fi

  docker compose --profile jobs build \
    data-operation data-collection data-gateway data-dispatcher data-reconciler \
    crawl-job browser-job

  docker run --rm --network none --read-only --init \
    --tmpfs /tmp:rw,noexec,nosuid,size=536870912 \
    --cap-drop=ALL --security-opt no-new-privileges \
    --cpus=1 --memory=2g --pids-limit=128 --shm-size=256m \
    --entrypoint /app/crawl-job data-platform-browser-job:dev \
    --browser-self-test

  if test -n "$(docker ps -q --filter label=data-platform.owner=dispatcher)"; then
    echo 'A Data Job is running. Wait for completion, then use the service-update command below.'
    exit 1
  fi
  docker compose up -d --no-deps \
    data-operation data-collection data-gateway data-dispatcher data-reconciler

  python tests/live_collection_smoke.py --browser-fixture
)
```

DB 검사에서는 임의의 별도 schema를 사용한다. 새 Collection은 `source_documents.browser_capture` JSONB 컬럼만 추가한다. 원본 ID·해시·이전 요청의 fingerprint를 유지한다. 브라우저 요청이 생긴 뒤에는 이를 모르는 이전 Operation/Dispatcher로 되돌리지 않는다.

이미지 빌드와 실제 Job 제한 검사는 모두 UID 65532로 Go→Python→Chromium→Go 응답 전달을 검사한다. 외부 네트워크 없이 작성한 JavaScript가 텍스트를 바꾸고 첨부 링크를 생성하는지, 최초 응답을 보존하는지, 사설/다른 호스트 요청이 HTTP 클라이언트에 도달하지 않는지 확인한다. 이미지 빌드 검사는 설치 상태, `docker run` 검사는 read-only·tmpfs·메모리·PID 등 실제 실행 제한을 검증한다. 실패 시 제한을 해제하지 말고 출력된 단계(`network_isolation`, `browser_start`, `navigation` 등)를 공유한다.

실행 중인 Data Job 때문에 서비스 교체만 중단됐다면 빌드·검사를 반복하지 않고 Job 종료 후 다음만 실행한다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/data_platform
  docker compose up -d --no-deps \
    data-operation data-collection data-gateway data-dispatcher data-reconciler
  python tests/live_collection_smoke.py --browser-fixture
)
```

## 2. 실제 수집 판정

`--browser-fixture`는 작성한 작은 HTML을 공개 [httpbin base64 엔드포인트](https://httpbin.org/#/Dynamic_data/get_base64__value_)로 제공받는다. 브라우저 설정이나 내부 주소 허용을 바꾸지 않는다. 외부 서비스 상태에 따라 429/5xx·접근 오류가 날 수 있으며 이때 실제 수집 성공으로 기록하지 않는다.

- 초기 응답에는 완성된 검사 문구가 없고, JavaScript 실행 후 DOM에는 `BROWSER-DYNAMIC-CONTENT-20260915`가 있어야 한다.
- JavaScript가 만든 첨부 링크를 따라 두 번째 문서를 등록하고 정확한 `browser attachment bytes`를 확인한다.
- DOM·초기 HTTP 응답·첨부의 다운로드와 해시, 동일 키 재접수·변경 충돌, Operation/CPU Job 1개와 성공 종료, 실제 이미지 ID를 확인한다.
- 마지막에 `PASS: JavaScript changed the DOM...`와 전체 수집 PASS가 나와야 한다.
- 보고서는 `tests/results/collection/<키>.json`, 서버 Go 결과는 `tests/results/browser/server-go-tests.log`다.

`--browser-fixture` 결과와 실제 운영 웹페이지의 수집 품질은 구분한다. 원하는 공개 HTML에는 다음처럼 적용한다.

```bash
python tests/live_collection_smoke.py \
  --url 'https://수집할-공개-호스트/페이지' \
  --render-javascript --render-wait-ms 3000 \
  --include-attachments --max-documents 3
```

위 URL은 설명용이므로 실제 URL로 바꾼다. 링크 페이지도 실행하려면 `--follow-links --max-depth 1`을 추가한다. 페이지가 많으면 전체 120초 제한에 걸릴 수 있다. CAPTCHA·로그인·클릭/폼 제출·무한 스크롤·새 창·iframe·Service Worker·WebSocket을 사용하는 기능은 이번 범위에 포함하지 않는다.

## API·결과

```json
{
  "name": "동적 자료 수집",
  "url": "https://example.com/documents/",
  "options": {
    "render_javascript": true,
    "render_wait_ms": 1500,
    "include_attachments": true,
    "max_documents": 3,
    "timeout_seconds": 120
  }
}
```

| 필드/경로 | 의미 |
|---|---|
| `options.render_javascript` | 기본 false. true일 때 브라우저 Job 선택 |
| `options.render_wait_ms` | load 이후 대기. 기본 1000ms, 1~10000ms. 0은 기본값. 브라우저 모드 필요 |
| `processor_version` | 브라우저 실행의 모든 결과는 `chromium-dom-v1` |
| `browser_capture` | 렌더링 HTML에만 존재. 첨부는 null |
| `representation` | `rendered_dom` |
| `response_sha256`, `response_size_bytes`, `response_content_type` | 최초 최종-리다이렉트 HTML 응답의 무결성·형식 |
| `response_artifact_uri` | 서버가 만든 최초 응답 객체 주소 |
| `network_requests`, `network_bytes` | 페이지 HTML과 렌더링에 전달한 HTTP 리소스의 요청 수·본문 바이트. 전체 wire 트래픽 측정값이 아님 |
| `blocked_requests` | 다른 호스트·비지원 요청 등 브라우저에서 거절한 요청 수. 0보다 크면 일부 기능이 빠질 수 있음 |
| `GET /api/source-documents/{id}/content` | DOM 또는 HTTP 첨부/기존 원본. 검증 후 attachment로 다운로드 |
| `GET /api/source-documents/{id}/http-response` | 보존된 초기 응답. 브라우저 DOM 문서 외에는 404 |

저장 총량은 **DOM + 보존된 HTTP 응답 + 첨부**를 합산한다. 각각 `max_bytes` 이하여야 하며 합계가 `max_total_bytes`를 넘으면 실패한다. 브라우저만 켠 기본 요청은 문서 1개/총 16MiB, 링크·첨부 확장 기본 문서 수는 기존 10개다. 총량 기본식은 브라우저 모드에서 `min(max_bytes × max_documents × 2, 64MiB)`다. 나머지 깊이/개수 상한은 HTTP 확장과 같다.

페이지마다 새 브라우저/빈 context를 생성한다. load 이후 지정한 시간의 DOM 스냅샷이며 페이지의 모든 비동기 동작이 영구히 끝났다는 의미는 아니다. JavaScript 오류, 필요한 같은 호스트 리소스의 HTTP 오류, 렌더링 중 주 문서 URL 변경, 용량/시간 초과는 실패한다. 일부 사이트의 누락된 이미지 등도 리소스 실패를 일으킬 수 있다. 페이지를 클릭하거나 로그인 상태를 불러오지 않는다.

## 네트워크·실행 제한

- Chromium/Python 자식과 후손에 Linux seccomp를 먼저 적용해 AF_UNIX 외의 소켓 생성과 io_uring을 거절한다. 필터 실패 시 실행을 중단한다.
- Playwright route는 응답을 채우거나 거절하기만 한다. 브라우저의 직접 GET·proxy 우회·자동 쿠키 전달은 없다. HTTP 리소스는 Go에서 공개 IP 검사 후 같은 확인 주소로 연결한다.
- seed의 정확한 hostname과 HTTP 80/HTTPS 443만 지원한다. 모든 리다이렉트에도 적용하고 HTTPS 하향 이동을 거절한다. CDN/서브도메인은 자동 허용하지 않는다.
- GET만 전달하며 browser request의 Cookie·Authorization·사용자 입력 헤더를 전달하지 않는다. Set-Cookie도 반환하지 않는다.
- 페이지별 전달은 초기 HTML 포함 128요청/본문 32MiB, 렌더러 전체는 512요청/64MiB로 제한한다. 첨부와 초기 HTTP 다운로드는 기존 수집 바이트/시간 제한도 적용한다. 실패 과정에서 다운로드한 뒤 거절된 초기 응답과 HTTP 리다이렉트 처리량은 이 렌더링 지표와 별개다.
- 1 CPU, 메모리 2GiB, PID 128, tmpfs 512MiB, read-only, no-new-privileges, cap-drop ALL, GPU 없음. 실패한 Job/로그는 기존 방식으로 보존한다.
- Chromium의 내부 user-namespace sandbox를 켜기 위해 컨테이너 권한을 확장하지 않는다. 컨테이너 및 자식 프로세스 네트워크 필터를 실행 경계로 사용한다. 다중 사용자 외부 공개 운영의 완전한 격리/인증 기능을 구현한 단계는 아니다.

구현 참고: [Playwright 네트워크 interception](https://playwright.dev/python/docs/network), [BrowserContext route·Service Worker 정책](https://playwright.dev/python/docs/api/class-browsercontext), [버전 고정과 Docker 실행](https://playwright.dev/python/docs/docker). Python 패키지/다운로드 브라우저는 Playwright 1.62.0으로 맞춘다.

## 재개와 다음 단계

대기 시간 초과·응답 유실이면 출력된 보고서를 그대로 사용한다.

```bash
python tests/live_collection_smoke.py \
  --resume tests/results/collection/실제-보고서-키.json
```

브라우저 보고서는 요청·키·API·기대값의 해시를 저장하고 변경되면 API 호출 전에 중단한다. 같은 키는 원래 실행만 확인하며 failed 실행을 새로 시작하지 않는다. 새 실행이 필요하면 실패 원인을 먼저 확인한다. 렌더링/다운로드는 자동 재실행하지 않고, 동일 완료 manifest만 재전송한다. 저장된 DOM과 최초 응답을 전체 검증한 뒤 하나의 DB 트랜잭션으로 등록한다.

브라우저 수집과 Streamlit의 서버 검사는 완료했다. 현재 남은 화면·운영 문서 확인은 [Data 상태](status.md)를 따른다. 완료한 OCR import·배포·합성 품질 검사를 문서 변경만으로 재실행하지 않는다.
