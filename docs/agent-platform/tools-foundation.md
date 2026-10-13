# A03 공통 Tool·MCP 기반 — 구현·실행 안내

> 이 문서는 A03 인수 당시의 기능/절차다. 현재 A04 OpenAPI·HTTP·최소 UI·서버 적용은 [A04 안내](openapi-foundation.md)를 기준으로 한다.

갱신일: 2026-09-23. **A03 구현·로컬 검증·PostgreSQL 16/Compose 서버 인수 완료(2026-09-23)**. A02 인수는 완료된 과거 근거로 유지한다. 다음 기능은 A04 OpenAPI 등록이다. [검증 기록](../history/agent-platform-202609.md#testing-tools-20260922), [설계 계약](api-tools.md), [작업계획](work-plan.md)을 함께 따른다.

## 1. 구현한 기능과 플랫폼 경계

| 구성 | 현재 동작 |
|---|---|
| Agent 관리 서비스 | Tool·ToolVersion·Connection/불변 연결 버전, 발행/폐기/비활성화, revision 충돌·멱등 접수, ToolCall DB 기록 |
| Agent 작업자 | 관리 서비스 내부 2개 작업자가 Agent DB 대기열에서 실행을 승인하고 Python runtime을 gRPC 호출 |
| Python runtime | 고정된 진단 Tool 두 종류 실행, 연결 비밀 참조/명시적 credential revision 확인. DB 접근 없음 |
| MCP 서비스 | 공식 Go SDK **1.8.0**, **2025-11-25** 프로토콜, stateless Streamable HTTP·JSON 응답, 목록·호출 |
| Gateway | 기존 포트 **16100**, REST와 **`/mcp`** 제공. MCP를 private network의 전용 서비스로 전달 |
| Agent DB | 기존 `001_foundation.sql` 유지, `002_tools.sql` 추가. schema version **2** |

상시 서비스는 기존 4개와 `agent-mcp`를 합쳐 5개다. 선택적 `agent-db-checks`는 검사 후 종료한다. MCP·관리 서비스·DB는 Agent private network만 사용한다. Gateway/runtime만 기존 `ops-infra`에 연결하며 공개 포트는 Gateway의 16100 하나다. MCP에 DB나 대상 API 비밀을 주지 않는다. Model/Data/Knowledge/infra 실행 코드는 바꾸지 않았다.

Tool 정의·버전·연결·실행 기록은 Agent DB가 유일한 기준이다. REST와 MCP의 별도 Tool 등록부를 만들지 않았다. n8n/LLM·모델 배포·검색 인덱스가 없어도 A03 진단 Tool을 사용할 수 있다.

## 2. 현재 실행 가능한 Tool

| kind | 입력·binding | 결과와 용도 |
|---|---|---|
| `diagnostic_echo_v1` | 입력 `{ "text": "문자열" }`. binding의 `prefix`, `delay_ms`, `fail`은 진단 설정 | `result.data.text`. 성공·고정 버전·timeout·확정 실패 검사 |
| `diagnostic_connection_v1` | 입력 `{}`. binding의 `connection_version_id` 필수 | `configured`, 연결 버전 ID, credential revision. 연결 설정/비밀 참조를 확인하며 **대상 URL에 요청하지 않음** |

입출력 schema는 kind별 서버 정의로 고정되며 ToolVersion과 MCP 목록에 같은 입력 schema를 제공한다. 임의 schema·Python/shell 코드·URL 덮어쓰기·동적 adapter 등록은 받지 않는다. 일반 HTTP Tool/OpenAPI 변환은 A04, Knowledge 검색은 A05, LangGraph Agent는 A06 범위다.

Echo의 입력은 최대 4,096 Unicode 문자, prefix는 256자, delay는 0~30,000ms다. `timeout_ms`는 기본 30,000, 범위 100~30,000이며 **DB 접수부터 대기 시간을 포함**한다. 요청 JSON 전체 상한 256KiB와 중복 키·알 수 없는 필드·null 거절을 적용한다. 현재 출력은 작은 진단 JSON이며 2MiB 일반 HTTP 응답 처리는 A04 범위다.

## 3. 관리와 실행 계약

현재 구현한 REST는 [Tool API 표](api-tools.md)의 Tool/ToolVersion/Connection/ToolCall 경로다. OpenAPIImport 경로는 아직 구현하지 않았다. 추가 조회는 `GET /api/connections/{id}`, `GET /api/connection-versions/{id}`다. Tool/Connection 상세에는 버전 목록을 포함하며 최상위 목록은 기본 20·최대 100, UUID `cursor`를 사용한다.

- POST에는 UUID `Idempotency-Key`가 필수다. 동일 JSON의 키 순서/공백 차이는 정규화하고 같은 키의 다른 요청은 `409 KEY_CONFLICT`다. 같은 키의 정상 재전송에는 `Idempotency-Replayed: true`를 준다.
- A03은 배포 단위 서비스 토큰 한 개의 호출자 범위다. 토큰 교체로 과거 업무 키를 새 호출로 바꾸지 않는다. 다중 사용자/고객별 권한·OAuth 위임은 미구현이다.
- Tool 수정과 ToolVersion 생성은 **Tool의** `expected_revision`, 발행/폐기는 **ToolVersion의** `expected_revision`, 연결 버전 생성은 **Connection의** `expected_revision`을 사용한다. 버전 생성 시 부모 Tool/Connection revision이 증가한다.
- 버전 내용은 수정하지 않는다. ToolVersion 상태는 draft → published → retired다. draft 실행은 `mode=test`, published 실행은 `mode=run`이다. 비활성화/retired 뒤 **새 키**의 실행을 거절한다.
- `POST /api/tool-calls`는 입력·고정 버전·연결 snapshot과 호출 기록을 DB transaction에 저장한 뒤 **202**를 반환한다. `Location`의 GET으로 상태·결과를 읽는다. 결과는 `result.data`, 완료 구분은 `result.completion`, runtime identity는 `result.runtime_instance_id`다.
- REST `Idempotency-Key`와 MCP `invocation_key`는 같은 논리 호출 키 공간을 사용한다. 같은 버전·입력·mode·caller_context이면 같은 ToolCall이다. MCP는 mode=run·빈 caller_context를 사용한다.
- 이미 접수한 호출의 재전송은 Tool 비활성화/폐기 후에도 원래 접수 ID를 반환한다. 저장된 결과를 새 실행으로 덮어쓰지 않는다. 실제 재실행은 명시적으로 새 키를 사용한다.
- 관리 및 MCP 메모리/응답 크기를 제한하기 위해 Tool/Connection당 100개 버전, 배포 전체의 published+retired MCP 버전 합계 1,000개를 A03 상한으로 둔다. 상한 초과는 `409 TOOL_LIMIT_REACHED`이며 과거 버전을 자동 삭제하지 않는다.

발행된 Tool 예시는 다음 형태다. ID는 실행 결과의 실제 값을 사용한다.

```json
{
  "expected_revision": 1,
  "kind": "diagnostic_echo_v1",
  "binding": {"prefix": "확인:"},
  "timeout_ms": 30000
}
```

## 4. 대기·중단·결과 보존

| 상태 | 처리 |
|---|---|
| accepted | DB에 저장된 대기 작업. 재시작한 작업자가 기한 안에 승인 가능 |
| running | 고유 lease token과 deadline+10초의 lease를 가진 작업자가 실행 중 |
| succeeded | 응답 identity·출력 schema 검증 후 결과 저장 |
| failed | 입력 이후의 확정 오류·비밀 미설정·timeout. 안정된 error code 저장 |
| interrupted | running의 lease 만료. `TOOL_EXECUTION_INTERRUPTED`로 끝내고 자동 재실행하지 않음 |

Go 작업자는 `FOR UPDATE SKIP LOCKED`로 대기 작업을 선택한다. 완료 보고에는 lease token·running 상태·유효 기간을 검사해 예전 작업자의 늦은 결과를 거절한다. HTTP/MCP 접속 종료는 저장한 ToolCall을 삭제하지 않는다. 실행 자체를 재시도하지 않으며 결과 저장만 제한적으로 재시도한다. 기한이 지난 accepted 작업은 Python을 호출하지 않고 timeout으로 끝낸다.

현재는 외부 효과가 없는 진단 Tool만 지원한다. HTTP의 `external_pending`/`unknown`, 부모 AgentRun/StepRun·LangGraph checkpoint는 A04~A06에서 추가한다. A02 SystemCheck를 ToolCall로 전용하지 않았다.

## 5. MCP 연결

접속 URL은 `http://<서버 주소>:16100/mcp`이며 기존 Agent API Bearer 토큰을 사용한다. MCP 클라이언트의 initialize 협상 버전은 `2025-11-25`다. SDK의 다른 프로토콜 지원 여부와 이 배포가 선택한 버전을 구분한다. HTTP+SSE legacy endpoint나 stdio 프로세스 실행은 제공하지 않는다.

MCP name은 `t_<하이픈 없는 tool_id>_v<정수 버전>`이다. arguments envelope는 다음과 같다.

```json
{
  "invocation_key": "논리 호출마다 생성·보존한 UUID",
  "arguments": {"text": "연결 확인"}
}
```

목록에는 enabled·published 버전만 보인다. disabled/retired 버전의 호출 handler는 기존 키 재조회용으로 유지하고 새 호출 여부는 관리 서비스가 다시 판단한다. `structuredContent`와 호환 텍스트에 같은 ToolCall 기록을 반환한다. 실행 실패·대기 timeout은 `isError=true`와 코드/조회 가능한 호출 ID로 구분한다. 관리 API는 MCP Tool 목록에 공개하지 않는다.

Origin이 없는 서비스 클라이언트는 허용한다. 브라우저가 보낸 Origin은 기본 거절하며 필요한 경우 Agent `.env`의 `AGENT_MCP_ALLOWED_ORIGINS`에 공백으로 구분한 **정확한 origin**을 설정하고 MCP 컨테이너를 갱신한다. 서비스 토큰 인증을 함께 적용한다.

## 6. Connection과 비밀 참조

Connection에는 base_url, auth_type(none/bearer/api_key), credential_ref와 credential_revision을 저장한다. API Key에는 api_key_in(header/query), api_key_name을 둔다. URL에 사용자 정보/query/fragment를 넣지 않으며 localhost·loopback·link-local 등의 직접 등록을 거절한다. DNS 해석/redirect/HTTP 직렬화 검증은 실제 HTTP adapter를 추가하는 A04 작업이다.

실제 비밀은 운영자가 관리하는 Agent 파일 `.secrets/tool-credentials.json`에만 둔다. API에는 비밀 원문을 받거나 돌려주는 기능이 없다. 최초/업그레이드 기본값은 빈 객체 `{}`다. none 연결 진단은 비밀이 없어도 성공하며 bearer/api_key의 미설정 참조는 `CREDENTIAL_NOT_CONFIGURED`로 기록한다.

```json
{
  "example_api": {
    "revision": "v1",
    "value": "운영자가 별도로 설정할 실제 값"
  }
}
```

비밀 변경은 새 revision으로 등록하고 이를 가리키는 새 ConnectionVersion/ToolVersion을 만든다. 같은 참조의 revision을 바꾸면 이전 버전 호출은 `CREDENTIAL_REVISION_UNAVAILABLE`로 실패하며 최신 비밀로 조용히 대체하지 않는다. 디렉터리 0700·파일 0444와 runtime 전용 secret mount를 유지한다. 파일을 atomic replace했으면 Compose secret bind가 새 파일을 가리키도록 runtime을 재생성한다. 위 예시 값은 자격 증명이 아니며 검사에서 실제 외부 API 자격은 필요하지 않다.

## 7. 기존 A02 서버 업그레이드

전달 패치를 `~/workspace`에 내려받은 뒤 아래를 실행한다. 설정/기존 비밀/DB 볼륨을 유지한다. `upgrade_a03.py`는 없는 경우 빈 Tool 비밀 참조 파일 하나만 만든다. 기존 파일이 있으면 보존한다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_a03_20260922.tar.gz
python3 agent_platform_a03_20260922/apply_patch.py \
  --project-root ~/workspace/agent_project --apply

cd ~/workspace/agent_project/agent_platform
python3 ops/upgrade_a03.py
python3 ops/preflight.py --skip-port-check

docker build --target tests -t agent-platform-go-tests:a03.1 .
docker build -f runtime/Dockerfile --target tests -t agent-platform-python-tests:a03.1 .
docker compose up -d --build --wait

python3 ops/smoke.py --check-id 8a441176-a891-481e-87da-2e73e1762777
python3 ops/smoke_tools.py
docker compose --profile checks run --build --rm agent-db-checks
)
```

위 A02 check ID는 사용자의 현재 서버에서 확인한 값이다. 새 설치는 `configure.py`→`preflight.py`를 사용하며 A02 조회 대신 `python3 ops/smoke.py`를 실행한다. 기존 서버에서 configure.py를 다시 실행하지 않는다.

002 migration은 새 테이블을 추가하고 기존 SystemCheck를 보존한다. A02 이미지는 schema 2를 지원하지 않으므로 schema 업그레이드 뒤 이미지 태그만 A02로 바꾸는 방식의 롤백은 지원하지 않는다. runtime Probe의 내부 호환 표시는 `agent.platform.v1/a03`으로 바꿔 구 runtime 이미지 혼용 시 readiness가 실패하도록 했다.

`smoke_tools.py`는 진단 Tool 하나와 버전/호출을 만들고 REST/MCP 공통 경로를 검사한 뒤 Tool을 비활성화·버전을 폐기한다. 기록은 남긴다. DB 검사도 test 전용 실행 lane을 사용해 상시 작업자가 검사 호출을 가져가지 않게 하며 검사 정의를 종료 시 비활성화한다. 외부 모델/GPU/문서/인덱스를 호출하지 않는다.

## 8. 서버 인수와 다음 작업

위 검사가 통과하면 smoke 출력의 `tool_call_id`로 아래 재기동 조회를 수행한다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
docker compose restart agent-postgres agent-service agent-runtime agent-mcp agent-gateway
docker compose up -d --no-build --wait
python3 ops/smoke_tools.py --call-id 'smoke에서_출력한_tool_call_id로_교체'
)
```

`result=passed`, `mode=lookup`이면 서버의 기존 ToolCall 보존을 확인한 것이다. 2026-09-23 사용자 로그에서 기존 ToolCall `8e3b3160-8e29-49d8-96d2-e0540e67682e`·ToolVersion·request ID 유지까지 확인해 [A03 인수를 완료](../history/agent-platform-202609.md#testing-tools-20260922)했다. 완료된 서버 검사는 반복하지 않는다. 다음은 A04 OpenAPI 등록·분석·operation 선택·HTTP adapter·최소 UI이며 전체 Agent MVP 완료와는 구분한다.
