# A04 OpenAPI Tool — 구현·서버 적용

최신 후속: [직접 API 등록 / OpenAPI 가져오기](tool-registration-20260923.md) 패치 22개 파일 적용, Gateway 이미지 빌드·Healthy와 연결 전 화면 표시를 사용자 로그/화면으로 확인했다. test1 질문 임베딩 1회 및 검색 API 직접 호출에서 결과 5개를 확인했다. 이후 사용자 화면에서 Agent를 통한 Knowledge 검색 Tool 시험 성공(mode=test, state=succeeded)을 확인했다. 게시·발행 후 실행과 남은 A04 인수는 미확인이다. 내부 API schema 자동완성과 실행값 자동 연결은 후속 개선 요구로 추가했다. [자연어 검색 조합·서비스별 API 관리 방향](tool-composition-and-service-catalog-20260923.md)을 기록했으며 A05는 미구현이다.


갱신일: 2026-09-23. A04 구현·로컬 검증 완료, 사용자 서버 인수 대기. A02/A03 서버 인수 완료 기록은 유지한다. 이미지 `a04.1`, 계약 `agent.platform.v1/a04`, DB schema 3이다. [검증 범위](testing-openapi-20260923.md), [단계별 계획](work-plan.md)을 함께 따른다.

사용자는 최초 A04 화면 접속을 확인했다. 이후 요청에 따라 도구 목록·단계별 추가·실행 결과로 화면을 간소화했다. [화면 간소화·적용 방법](console-ux-20260923.md)을 우선 따른다. 이번 UI 로컬 동작 검사와 사용자 서버의 패치 적용·Gateway 빌드/Healthy·연결 전 화면을 확인했으며, 검색 Tool 시험 성공은 확인했으며 게시·발행 후 실행과 남은 A04 기능·DB 인수는 별도다. 아래 5절의 전체 A04 적용은 최초 설치/기능 갱신용이며 이미 A04가 실행 중이면 화면 문서의 Gateway 전용 명령만 사용한다.

## 1. 사용 흐름과 소유권

`http://<서버IP>:16100/console`에서 기존 Agent API 토큰으로 연결한다. 도구 목록 → 도구 추가 → 기능 선택 → 연결 설정 → 시험·사용 순서다. 기본 화면은 이름 기반 선택과 입력칸을 제공하고, 연결 버전·시간 제한·원시 구조·MCP 식별자는 상세에 둔다. 복합 입력은 JSON으로 작성한다. 시험 호출은 실제 대상 API를 실행하며 성공한 시험 기록이 있는 HTTP 초안만 사용을 시작할 수 있다.

토큰·최근 실행 목록·미확인 요청은 현재 페이지 메모리에만 보관한다. 새로고침 후 다시 연결하고, 과거 기록은 실행 ID로 조회한다. 비밀 원문 대신 credential 참조와 revision을 지정한다. 예제의 `api.example.com`은 실제 시험 서버가 아니다.

REST와 MCP는 같은 ToolVersion·입력 schema·ToolCall을 사용한다. MCP 주소는 `/mcp`, 도구 이름은 `t_<하이픈 없는 tool_id>_v<version>`이다. 발행 전이나 비활성화/폐기한 버전은 신규 MCP 목록에서 제외한다. 기존 호출 키 재조회는 유지한다.

명세 원본·분석·정의·버전·연결·실행 결과는 Agent DB가 소유한다. 원본 업로드는 파싱만 하며 외부 주소를 호출하지 않는다. 다른 플랫폼의 API를 연결할 수 있지만 그 DB/파일/인덱스/런타임 제어는 가져오지 않는다. Model/Data/Knowledge/infra 실행 코드와 GPU 자원은 변경하지 않았다. 상시 서비스는 기존 5개, 호스트 포트는 기존 16100이다.

## 2. 지원 범위와 명시적 제한

| 항목 | A04 구현 |
|---|---|
| 문서 | UTF-8 JSON 또는 YAML, OpenAPI 3.0.x/3.1.x. 원본 5 MiB, 200 operation, 배포당 100개 고유 명세 |
| 식별 | `METHOD /path`가 operation key. 같은 표시 이름/operationId도 Tool UUID와 버전으로 구분 |
| 참조 | 같은 문서의 JSON pointer `$ref`. 외부/순환 참조, `$ref`와 다른 필드의 병합은 미지원 |
| HTTP | GET/POST/PUT/PATCH/DELETE, 고정 Connection base URL + 고정 path template |
| 입력 | path 단일 값, query 단일 값 또는 form/explode 단일 값 배열, 일반 header 단일 값, application/json body |
| 이름 | `path`, `query`, `header`, `body` namespace 분리. 서로 같은 이름이어도 독립 |
| 응답 | 정확한 상태 번호의 동기 2xx JSON 또는 204 빈 응답. 선언된 202·2XX 범위 응답은 미지원 |
| 인증 | none, Bearer, API Key(header/query), operation override 및 OR 대안 선택. AND·Basic·OAuth/OIDC·cookie 미지원 |
| 주소 | servers의 operation→path→root 우선순위, 변수 default로 주소 후보 표시. 실제 연결은 사용자가 별도 확정 |
| 제외 | multipart·binary·SSE·WebSocket·callbacks/webhooks·자동 페이지 순회·장기 작업 polling·임의 코드 |

Schema는 object/array/string/integer/number/boolean/null, properties/required/additionalProperties/items/enum(100개 이하), 3.1 const/boolean schema를 지원한다. type은 한 종류와 선택적 null까지다. 3.0 nullable 및 exclusive bound를 정규화한다. 수치 최소/최대/배수, 문자열 길이, 배열/객체 크기, RE2 pattern(512자 이하), uuid/date/date-time/int32/int64/float/double format을 검사한다. `uniqueItems`는 명시적 `maxItems <= 1000`일 때만 지원한다.

allOf/oneOf/anyOf/not/조건 schema, readOnly/writeOnly=true, 미지원 format/keyword는 이유와 JSON pointer를 남기고 해당 operation 선택을 막는다. default/example/examples/deprecated 등 주석과 x- 확장은 실행 동작으로 해석하지 않는다. default 입력 자동 삽입이나 형변환을 하지 않는다. 응답 headers/links 메타데이터는 보관하지만 검증·후속 호출하지 않는다. 이것은 OpenAPI 전체 규격 검사기가 아닌 명시한 부분의 실행 변환기다.

JSON 중복 키/비정상 수치, YAML 다중 문서·앵커·별칭·사용자 tag는 거절한다. 문서 깊이 48/노드 100,000, schema 확장 깊이 20/노드 4,000, 분석 1 MiB/operation 64 KiB 제한이 있다. 숫자는 유한 float64 범위와 128자 표현 한도 안에서 Go 검증 시 정밀도를 보존한다. 브라우저 입력은 JSON 원문으로 전송한다.

## 3. REST 계약

기존 [A03 관리 API](tools-foundation.md)의 Bearer 토큰·UUID `Idempotency-Key`·revision·페이지 계약을 유지한다.

| API | 입력·결과 |
|---|---|
| POST /api/openapi-imports | multipart가 아닌 파일 원문 body. application/json 또는 application/yaml(application/x-yaml, text/yaml 허용). 201 import_id/hash/format/analysis |
| GET /api/openapi-imports | limit/cursor 페이지. 목록은 요약 정보 |
| GET /api/openapi-imports/{id} | 영속화한 분석·지원 여부·입력/응답 매핑. 원문 source와 비밀은 반환하지 않음 |
| POST /api/tools/{id}/versions | 아래 binding으로 불변 HTTP draft 생성 |
| POST /api/tool-calls | 기존 호출 접수. test는 draft, run은 published |
| GET /api/tool-calls/{id} | 실행 상태와 저장된 결과 조회 |

같은 원문 바이트 hash+format은 같은 import로 재사용한다. 재업로드한 다른 명세는 새 import이며 기존 버전은 바꾸지 않는다. 같은 멱등 키에 다른 문서를 넣으면 409다. 문서 전체 형식 오류는 400 OPENAPI_INVALID, operation 미지원은 분석에 남고 draft 등록 시 400 OPENAPI_UNSUPPORTED다. 파일 크기 초과 413, 미지원 Content-Type 415, 한도 초과 409다.

```json
{
  "expected_revision": 1,
  "kind": "openapi_http_v1",
  "binding": {
    "import_id": "<import UUID>",
    "operation_key": "POST /items/{id}",
    "connection_version_id": "<connection version UUID>",
    "auth_scheme": "none",
    "effect": "write",
    "forward_idempotency": false
  },
  "timeout_ms": 30000
}
```

`auth_scheme`은 분석의 auth_options 중 선택한 이름이다. Connection의 인증 종류·API Key 위치/이름과 일치해야 한다. binding.http와 입출력 schema는 서버가 생성하므로 클라이언트가 덮어쓸 수 없다. 기존 Tool에 새 버전을 만들 때는 최신 Tool revision을 사용한다.

```json
{
  "tool_version_id": "<tool version UUID>",
  "mode": "test",
  "arguments": {
    "path": {"id": "one"},
    "query": {"id": ["a", "b"]},
    "body": {"id": null}
  }
}
```

입력은 선택한 명세에 맞춰 작성한다. 위 예시는 body가 nullable을 허용할 때만 유효하다. REST 호출은 202 접수 응답 후 ToolCall을 조회한다. MCP는 invocation_key와 arguments를 받으며 같은 키/내용으로 REST와 MCP에서 호출해도 기존 ToolCall 하나를 반환한다.

## 4. HTTP 실행·비밀·실패

입력 요청 전체 256 KiB, 외부 응답 2 MiB, URL/헤더 합계 각각 16 KiB, 일반 헤더 값 4,096자까지다. 기본 deadline 30초·상한 120초이며 DB 대기 시간을 포함한다. DNS/연결 각각 최대 5초이되 남은 전체 시간을 넘지 않는다. 대기 만료는 외부 요청을 보내지 않는다.

모든 호출은 자동 retry 0회·redirect 추종 0회다. OS proxy 설정을 사용하지 않는다. TLS 인증서/호스트를 검증한다. DNS의 모든 응답 IP를 검사한 뒤 선택 IP로 연결하며, loopback/link-local/metadata/멀티캐스트 등은 차단한다. 사설 IP는 명시적 연결을 위해 허용한다. localhost·URL 사용자정보·query/fragment·인코딩된 base path·dot 경로는 등록 거절한다. 호출자가 URL/Host/Authorization/Cookie 등 예약 헤더를 바꾸지 못한다. path 값의 slash/backslash/percent/dot segment/개행을 거절하고 query는 별도 인코딩한다.

인증 원문은 기존 `.secrets/tool-credentials.json`의 `{"참조":{"revision":"v1","value":"실제값"}}` 형식으로 운영자가 설정한다. 런타임만 이 파일을 읽는다. Connection과 호출 snapshot에는 참조/revision만 고정한다. 실제값을 바꾸면 새 revision/ConnectionVersion/ToolVersion을 사용한다. 파일 교체가 컨테이너에 보이지 않으면 runtime을 재생성한다. 누락/불일치는 CREDENTIAL_NOT_CONFIGURED/CREDENTIAL_REVISION_UNAVAILABLE로 기록된다. 응답에 반사된 해당 credential 문자열은 마스킹하며 오류의 원시 body는 보관하지 않는다. 명세 예제에 실제 비밀을 넣지 않는다.

`effect` 미지정은 write다. HTTP method만 보고 read로 확정하지 않는다. `forward_idempotency=true`는 대상이 해당 계약을 제공할 때 선택하며, Agent의 호출 키를 `Idempotency-Key` 헤더로 전달한다. 임의 키 값 매핑은 지원하지 않는다. 명세가 그 헤더를 일반 입력 parameter로 선언한 operation은 현재 미지원이다. Agent의 중복 접수 방지가 대상 API의 정확히 한 번 실행을 보장하지는 않는다.

| ToolCall 상태 | 의미·처리 |
|---|---|
| succeeded | 명세의 동기 성공 상태와 JSON/schema 검증 통과. result={status,data,completion:completed,runtime_instance_id} |
| failed | 입력/인증/명시적 HTTP 거절/읽기 요청 오류. 안전한 status·오류 code만 보관 |
| external_pending | 예기치 않은 외부 202. TOOL_ASYNC_UNSUPPORTED, completion=accepted_only, 안전한 Location 참조(사용자정보/query/fragment 제거). 완료 판정/자동 polling 없음 |
| unknown | 이미 전송한 write의 timeout·단절·응답 검증 실패, 또는 실행 중 worker lease 유실. TOOL_EFFECT_UNKNOWN, completion=unknown. 자동 재실행 없음 |
| interrupted | 실행 중 read/기존 진단 작업의 lease 유실. 자동 재실행 없음 |

unknown/external_pending은 MCP에서도 성공으로 반환하지 않는다. 같은 키는 기존 호출을 조회한다. 새 키로 다시 실행하기 전에는 외부 시스템에서 효과를 확인한다. DB 접수 성공과 외부 처리 성공, 스키마 일치와 업무 결과의 의미는 별개다.

## 5. 인수를 마친 A03 서버에 적용

패치 적용기는 경로/해시를 먼저 검증하고 충돌 시 쓰기 전에 중단한다. 기존 설정·비밀·DB 볼륨을 유지한다. configure.py를 다시 실행하지 않는다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_a04_20260923.tar.gz
python3 agent_platform_a04_20260923/apply_patch.py \
  --project-root ~/workspace/agent_project --apply
cd ~/workspace/agent_project/agent_platform
python3 ops/preflight.py --skip-port-check
docker build --target tests -t agent-platform-go-tests:a04.1 .
docker build -f runtime/Dockerfile --target tests -t agent-platform-python-tests:a04.1 .
docker compose up -d --build --wait
python3 ops/smoke_tools.py --call-id 8e3b3160-8e29-49d8-96d2-e0540e67682e
python3 ops/smoke_openapi.py
docker compose --profile checks run --build --rm agent-db-checks
)
```

A03 호출 ID는 이 사용자 서버에서 이미 확인한 기록이다. 신규 설치에서는 그 조회를 제외한다. checks는 Agent 전용 DB·Python runtime·검사 컨테이너의 임시 HTTP fixture만 사용하며 다른 플랫폼 API/GPU를 호출하지 않는다. 로그의 `A04 real HTTP ... persisted call ...; version ...` 식별자를 보관한다.

첫 적용이 통과하면, 위 로그의 A04 HTTP call ID로 다음 재기동 조회를 한 번 확인한다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
docker compose restart agent-postgres agent-service agent-runtime agent-mcp agent-gateway
docker compose up -d --no-build --wait
python3 ops/smoke_tools.py --call-id <위에서 출력된 A04-HTTP-call-ID>
)
```

화면에서 본인 명세의 등록→시험→발행도 확인한다. 자동검사는 지원 범위의 fixture 검사이며 모든 실제 API와의 호환 인수가 아니다. 서버 결과를 받은 뒤 A04 인수 상태를 갱신하고 A05 Knowledge 검색 Tool로 진행한다.

새 설치는 기존 `ops/configure.py`로 독립 설정/비밀을 생성한 다음 위 빌드·기동 절차를 따른다. A02에서 바로 올리는 경우에만 기존 `ops/upgrade_a03.py`로 없는 credential 파일을 추가한다. `003_openapi.sql`은 새 원본 테이블과 상태 제약을 추가한다. 001/002와 과거 데이터는 유지하며, schema 3 이후 구 A03 이미지만 실행하는 롤백은 지원하지 않는다.
