# Tool 등록·OpenAPI·MCP·검색 계약

갱신일: 2026-09-23. A01 설계 기준 중 **A03 Tool/ToolVersion/Connection/ToolCall·MCP의 구현·로컬 검증·서버 인수를 완료했다**. A04 OpenAPIImport/HTTP adapter·최소 UI도 구현·로컬 검증을 마쳤으며 서버 인수 대기다. 현재 payload·지원 부분·상한·실행법은 [A04 안내](openapi-foundation.md), 기존 공통 관리 계약은 [A03 안내](tools-foundation.md)를 따른다. Knowledge 검색은 A05 예정으로 아직 실행 adapter가 아니다. 기존 API는 [Model 임베딩](../reference/embedding.md), [Knowledge 검색](../knowledge-platform/api-search.md)을 따른다.

## 1. Tool의 정체성과 수명주기

Tool은 Agent Platform 고유 리소스다. 이름·설명·입출력·연결 대상·호출 정책·버전의 기준 저장소를 Agent가 소유한다. MCP/REST/n8n은 같은 Tool 실행 서비스를 이용하며 별도 등록부를 만들지 않는다.

| 리소스 | 핵심 필드·역할 |
|---|---|
| Tool | tool_id, display_name, description, enabled, revision. 논리 식별자와 신규 사용 허용 여부 |
| ToolVersion | tool_version_id, tool_id, version, state, kind, input_schema, output_schema, binding, definition_sha256. 발행 후 불변 |
| OpenAPIImport | import_id, document_sha256, spec_version, 검증 결과·operation 목록. 가져온 문서의 불변 원본 |
| ConnectionRevision | connection_id/version, base_url, auth_type, credential_ref, 연결/응답 제한. 등록 시 확정한 연결 정보 |
| CredentialRef | 서버에 설정한 비밀 참조 이름. 실제 값은 schema·응답·n8n 워크플로 export에 넣지 않음 |
| ToolCall | tool_call_id, tool_version_id, invocation_key, input_sha256, state, 단계 시간, 결과/오류, 부모 AgentRun/외부 실행 참조 |

ToolVersion 상태는 draft → published → retired다. 발행 후 동작을 바꾸려면 새 draft/version을 만든다. retired와 Tool.enabled=false는 신규 호출을 막는다. 이미 수락한 호출의 버전/입력을 바꾸지 않는다. 자격 폐기·대상 API 중지 때문에 진행 중 호출이 실패할 수 있으며 이를 성공으로 덮지 않는다.

MVP는 비활성화/폐기 표시를 제공하고 실행 기록이 참조하는 버전을 물리 삭제하지 않는다. 관리 변경은 expected_revision으로 동시 수정 충돌을 거절한다. Tool 표시 이름은 고유 ID를 대신하지 않는다.

## 2. REST API — A03·A04 구현

| 메서드·경로 | 역할 |
|---|---|
| POST /api/connections | base URL·인증 방식·서버 비밀 참조를 연결 설정으로 등록 |
| GET /api/connections, GET /api/connections/{id} | 비밀 값을 제외한 연결 목록·상세/버전 |
| GET /api/connection-versions/{id} | 고정 연결 버전 조회 |
| POST /api/connections/{id}/versions | 변경된 연결 설정의 불변 revision 생성 |
| POST /api/openapi-imports | A04 구현: 명세 파일 업로드·분석. API 호출이나 Tool 발행은 하지 않음 |
| GET /api/openapi-imports | A04 구현: limit/cursor 기반 요약 목록 |
| GET /api/openapi-imports/{id} | A04 구현: operation별 지원/미지원 이유·필수 입력·매핑 조회 |
| POST /api/tools | 논리 Tool 생성 |
| GET /api/tools, GET /api/tools/{id} | 목록·상세 |
| PATCH /api/tools/{id} | 표시 정보·enabled 변경, expected_revision 필수 |
| POST /api/tools/{id}/versions | 진단 및 openapi_http_v1 draft 생성. 검색 kind는 A05 |
| GET /api/tool-versions/{id} | schema·binding·상태·불변 hash 조회 |
| POST /api/tool-versions/{id}/publish | draft 검증 후 published 전환 |
| POST /api/tool-versions/{id}/retire | 신규 사용 종료 |
| POST /api/tool-calls | 명시적 ToolVersion 실행 접수. Idempotency-Key 필수, 저장 후 202 |
| GET /api/tool-calls/{id} | 실행 상태·결과·오류 조회 |

버전 수정은 새 draft 생성으로 처리한다. 시험 호출은 관리 화면에서 draft를 명시적으로 선택해 같은 ToolCall 실행 경로로 보내며 mode=test로 기록한다. draft는 MCP 목록이나 게시된 Agent에서 사용할 수 없다. 시험 호출은 실제 API 요청이므로 등록/목록 조회와 달리 외부 효과가 발생할 수 있다.

POST 생성·발행·실행에는 Idempotency-Key를 적용한다. 같은 호출자 범위·경로·키·정규화 입력은 같은 리소스/접수 응답을 재현하며 다른 입력은 409다. 관리 변경의 revision 검증과 실행 멱등 키를 구분한다.

실행 요청 예시:

    {
      "tool_version_id": "11111111-1111-4111-8111-111111111111",
      "arguments": {
        "path": {"item_id": "sample-1"},
        "query": {"lang": "ko"}
      },
      "mode": "run"
    }

GET 상태가 succeeded일 때 결과를 사용한다. 202 응답 자체는 Tool 실행 성공이 아니다. 모든 ID 예시는 형식 설명용이며 서버의 실제 자원이 아니다.

## 3. OpenAPI MVP 지원 범위

OpenAPI 3.0.x와 3.1.x의 아래 공통 부분부터 지원한다. 전체 규격 호환을 주장하지 않는다. 명세·operation·필드별 지원 여부를 화면에 표시한다.

| 영역 | 첫 구현 범위 |
|---|---|
| 입력 파일 | UTF-8 JSON/YAML, 단일 문서, 최대 5 MiB, 안전한 YAML 해석·중복 키 거절 |
| operation | GET/POST/PUT/PATCH/DELETE 중 사용자가 선택한 operation. method/path를 고유 기준으로 쓰고 operationId는 이름 후보 |
| 참조 | 같은 문서의 components 등 local $ref. 원격 참조·순환 참조는 미지원 표시 |
| schema | object/array/string/integer/number/boolean/null, required·enum·기본 수치/길이 제한 등 명시적으로 구현한 부분 |
| 정규화 | 3.0 nullable와 3.1 null 허용 표현을 구분해 변환. 미지원 키워드는 버리지 않고 발행 거절 |
| 복합 schema | allOf/oneOf/anyOf/not, 조건 schema 등은 초기 미지원 |
| parameters | path의 단일 값, query의 단일 값 및 form/explode 배열, 허용된 일반 header의 단일 값 |
| requestBody | application/json 객체/배열 등 지원 schema. path/query/header/body를 별도 namespace로 유지 |
| 응답 | 명세에 정의한 동기 2xx JSON, 204 빈 응답. 응답 전체를 schema 확인 후 결과 envelope에 저장 |
| 인증 | none, API Key(header/query), HTTP Bearer. 한 operation에서 선택한 한 인증 방식 |
| 제외 | cookie 인증, OAuth/OIDC 위임, Basic, multipart·바이너리·SSE/WebSocket, callbacks/webhooks, 자동 pagination |

3.0 schema를 MCP의 JSON Schema로 단순 복사하지 않는다. 변환한 schema와 HTTP 직렬화 규칙을 함께 검사한다. 지원 키워드 목록은 [A04 구현 안내](openapi-foundation.md)에 고정하며 미지원 operation은 선택 불가 사유를 남긴다.

기존 Go 플랫폼 API가 OpenAPI 명세를 자동 제공한다고 가정하지 않는다. 기존 플랫폼 API를 일반 변환기로 등록하려면 해당 소유자가 관리하는 명세가 필요하다. A05 built-in 검색 Tool은 이미 문서화된 공식 API 계약으로 직접 연결하므로 기존 플랫폼 전체의 OpenAPI 문서화가 선행 조건은 아니다.

등록 절차는 업로드·분석 → operation 선택 → 연결/입력/출력 매핑 검토 → Tool draft → 명시적 시험 호출 → 발행이다. 이름 충돌은 서버가 생성한 ID/버전으로 구분하며 기존 Tool을 덮어쓰지 않는다. 원본 spec 변경은 재가져오기와 새 ToolVersion으로 반영한다.

인증의 global/operation override와 무인증 override를 해석한다. OR 대안은 사용자가 한 방식을 선택하고 복수 scheme 동시 요구(AND)는 MVP 미지원으로 표시한다. 비밀은 배포 시 서버 비밀 참조로 설정하고 UI에서는 참조를 선택한다. 런타임 credential 값 교체는 별도 revision으로 기록하며 과거 결과에 원문 비밀을 보존하지 않는다.

HTTP method만으로 조회/부작용을 확정하지 않는다. binding에 effect=read 또는 write를 명시하고 미지정은 write로 취급한다. 자동 재시도는 양쪽 모두 기본 0회다. 대상 API가 지원하는 Idempotency-Key 전달은 검증된 매핑에 한해 설정하며 Agent DB 멱등성이 외부 API 중복 효과까지 자동 방지하는 것은 아니다.

## 4. 연결과 HTTP 실행

호출자는 실행 인자로 임의 URL·Host·Authorization을 바꾸지 않는다. base URL과 path template은 ConnectionRevision/ToolVersion에 고정한다. servers 변수가 있으면 등록 시 실제 값으로 확정하고 사용자가 검토한다. 등록된 주소·경로 범위를 넘는 redirect는 따르지 않는다. 내부 플랫폼 주소도 명시적으로 등록하며 loopback/metadata 등에 대한 임의 탐색을 제공하지 않는다.

- Tool 입력 JSON 최대 256 KiB, 응답 최대 2 MiB를 현재 상한으로 둔다. 대상 API의 더 작은 제한이 우선한다.
- 일반 HTTP Tool deadline은 기본 30초·상한 120초, connect timeout 5초다. 부모 Agent deadline을 넘지 않는다.
- path 값은 path segment로, query/header/body는 각 위치의 직렬화 정책으로 보낸다. 문자열 연결로 요청을 조립하지 않는다.
- HTTP transport 성공, JSON/schema 검증, 업무 완료를 구분한다. 알려진 오류 code와 status를 남기고 비밀이 섞일 수 있는 원시 응답 전체를 오류 로그로 출력하지 않는다.
- 202만 반환하는 장기 작업 API는 초기 등록 지원 대상에서 제외한다. 예기치 않은 202를 실제 수신하면 external_pending으로 기록하고 외부 참조를 보존한다. 외부 작업 성공/실패를 대신 판정하지 않으며 자동 polling·재요청을 하지 않는다.
- timeout/연결 단절 뒤 write의 처리 여부를 모르면 unknown으로 기록한다. 같은 키는 기존 기록을 반환하고 효과를 모른 채 새 호출을 만들지 않는다.

외부 응답의 body는 결과 envelope의 data에 두며 status와 completion=completed/accepted_only/unknown을 함께 둔다. 전송 성공을 Agent의 업무 성공으로 확대하지 않는다. AgentRun은 external_pending/unknown ToolCall을 정상 완료로 취급하지 않는다.

## 5. MCP 노출

MCP 서버는 Agent ToolVersion을 읽어 published·사용 가능한 도구를 노출한다. A03은 공식 Go SDK 1.8.0과 2025-11-25 프로토콜의 stateless Streamable HTTP로 tools/list·tools/call을 구현했다. 외부 MCP 서버 관리·stdio 프로세스 실행·프로토콜 자체 구현은 범위 밖이다.

- MCP name은 tool ID와 version으로 만든 안정된 이름(예: t_11111111111141118111111111111111_v1)을 사용한다. 표시 제목은 사용자 Tool 이름이다.
- MCP inputSchema는 invocation_key와 arguments envelope다. arguments는 발행된 ToolVersion schema와 일치한다.
- invocation_key는 호출자가 생성한 논리 호출별 UUID다. LangGraph 어댑터는 실행/단계의 저장된 키를 사용한다. MCP JSON-RPC request id를 업무 멱등 키로 사용하지 않는다.
- tools/call은 REST와 같은 ToolCall을 생성/조회하고 호출 deadline 안에서 결과를 기다린다. 수락 뒤 응답이 끊겨도 ToolCall은 보존한다.
- 성공 결과는 tool_call_id·실제 tool_version_id·결과/출처를 structuredContent와 호환 텍스트로 반환한다. 도구 실행 실패는 isError와 안정된 코드로, 프로토콜 오류는 SDK 계약으로 구분한다.
- external_pending/unknown/deadline은 일반 성공으로 반환하지 않는다. 조회 가능한 tool_call_id와 오류/완료 상태를 남겨 같은 키로 확인한다.
- 관리 REST 전용 기능을 MCP 목록에 자동 공개하지 않는다. Tool disabled/retired 정책을 MCP와 REST에서 동일하게 적용한다.

검색 Tool MCP 호출 인자:

    {
      "invocation_key": "22222222-2222-4222-8222-222222222222",
      "arguments": {"query": "이 문서의 주요 결론은 무엇인가요?"}
    }

## 6. built-in Knowledge 검색 Tool

kind=knowledge_search_v1은 Agent가 제공하는 API 조립 Tool이다. 대상 서비스들의 기능을 복제하지 않는다. 2026-09-23 사용자가 이 방향의 기록을 요청했다. [조합 위치·서비스별 API 관리와의 구분](tool-composition-and-service-catalog-20260923.md)을 따른다. A05 runtime 어댑터를 구현했다. [실제 binding·discovery·화면·제한](knowledge-search-a05-20260923.md)을 따른다.

| 구분 | 필드 |
|---|---|
| ToolVersion binding | Knowledge/Model 연결 참조, index_id, index_version_id, embedding_space_id, embedding_deployment_id, rerank 설정·profile, 검색 기본값·허용 범위 |
| 사용자 입력 | query 원문, 허용된 top_k/candidate_k, source_document_ids/canonical_ids/kinds 필터 |
| 결과 | ToolCall·버전·공간·실제 배포/profile, 본문·chunk ID·source spans·quality·원래 순위/점수·시간·빈 결과 이유 |

발행 화면에서 현재 active 버전을 선택할 수 있으나 저장 시 명시적 index_version_id로 고정한다. 실행 중 active가 바뀌어도 최신 버전으로 바꾸지 않는다. 다른 배포를 쓰려면 같은 공간인지 검증한 새 ToolVersion을 발행한다.

실행 순서:

1. Knowledge GET /api/indexes/{id}, GET /api/index-versions/{id}로 선택 버전의 소속·준비 상태·embedding profile을 확인한다.
2. Model GET /api/deployments/{id}/embedding-profile로 공간·역할·길이 계약을 확인한다.
3. Model POST /api/deployments/{id}/embeddings에 role=query, 원문 한 개, expected_embedding_space_id를 보낸다. 접두어는 Model이 적용한다.
4. 응답 역할·입력 ID·공간·차원·유한값을 대조한다.
5. Knowledge POST /api/indexes/{id}/search에 명시적 버전·공간·벡터·필터를 보낸다.
6. rerank ON일 때 query_text와 고정 reranker 배포/profile을 전달한다. OFF일 때 query_text는 보내지 않는다.
7. Knowledge가 정한 결과/순위/점수 의미·출처·품질을 보존해 반환한다.

기존 검색 제한인 top_k 1~50, candidate_k top_k~100, ON에서 명시적 candidate_k와 raw query_text 최대 8 KiB 등을 따른다. 질문 임베딩과 검색을 포함하는 Tool 전체 deadline은 기본 180초(rerank ON 기본 240초)·최대 240초이며 부모 deadline이 더 작으면 줄인다. 하위 API의 자체 deadline을 늘리지 않는다.

공간 불일치, 버전 삭제/미준비, 범위 밖 필터, 재정렬 실패를 오류로 반환한다. 빈 정상 결과는 no_matches로 유지한다. 다른 모델/버전·벡터 결과로 조용히 fallback하지 않는다. score를 확률로 취급하거나 근거 없는 공통 임계값으로 신뢰도를 표시하지 않는다.

## 7. 공통 오류와 검사

| 오류 범주 | REST 의미·동작 |
|---|---|
| TOOL_INPUT_INVALID / OPENAPI_UNSUPPORTED | 400, 필드/지원 불가 위치 표시 |
| TOOL_NOT_FOUND | 404 |
| TOOL_VERSION_UNAVAILABLE / CONNECTION_UNAVAILABLE / KEY_CONFLICT | 409 |
| TOOL_PAYLOAD_TOO_LARGE | 413 |
| TOOL_RESPONSE_INVALID | 502 |
| TOOL_UPSTREAM_UNAVAILABLE | 503 |
| TOOL_TIMEOUT | 504 |
| TOOL_ASYNC_UNSUPPORTED / TOOL_EFFECT_UNKNOWN | 호출 결과의 상태/코드, 외부 효과를 별도 기록 |

위 status는 접수 전 거절 또는 MCP/동기 결과 해석 기준이다. 이미 접수된 호출의 GET은 200으로 상태·오류 객체를 반환한다. 비밀·질문 원문·원시 백엔드 stack trace를 오류 메시지에 섞지 않는다.

지원 schema 변환·직렬화·인증 override·name 충돌·version 고정·응답 검증·중복/불명확 효과와 검색 API 경계를 A03~A05에서 검사한다. 실제 호출을 실행하지 않은 시험 결과를 성공 인수로 기록하지 않는다.

## 8. 규격 근거

- [OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html), [OpenAPI 3.0.3](https://spec.openapis.org/oas/v3.0.3).
- [MCP Tool 규격](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).

OpenAPI 링크는 A04 설계 근거다. A03의 SDK/프로토콜 선택과 실제 확인 범위는 [검증 기록](../history/agent-platform-202609.md#testing-tools-20260922)에 고정했다.
