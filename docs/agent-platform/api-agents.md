# Agent 정의·실행·n8n 연결 계약

갱신일: 2026-09-24. A06 `a06.1` / `agent.platform.v1/a06` / DB schema 4 구현 기준이다. [서버 적용·화면·검증](knowledge-qa-a06-20260924.md), [Tool 계약](api-tools.md)을 함께 따른다. A07은 [별도 계약/적용 안내](n8n-a07-20260924.md)에 구현했다. 최신 a07.1/schema 5에서도 아래 AgentRun 계약을 유지한다. A08은 Service/Gateway a08.1의 조회 목록·UI 확장이며 DB schema/실행 계약을 바꾸지 않는다. [A08 적용·화면 계약](console-a08-20260924.md).

## 1. 기준 정보·버전

| 리소스 | 현재 계약 |
|---|---|
| Agent | agent_id, display_name, description, enabled, revision, 시각. 논리 자산 |
| AgentVersion | agent_version_id, template_id/template_version, 단일 tool_version_id, model, prompt, context_max_chars, max_tokens, temperature, timeout_ms, 입출력 schema, definition_sha256 |
| AgentRun | agent_run_id, agent_version_id, request_id, invocation_key, input/definition hash, input, mode, 상태·결과·오류·시각, queue_ms/execution_ms |
| AgentStep | 부모 AgentRun 아래 step_id/position/attempt/state/시각/duration_ms/detail. attempt는 현재 1 |
| ToolCall | 공통 호출 기록에 parent_agent_run_id와 parent_step_id=search를 연결. 기존 독립 호출은 부모 필드 없음 |
| WorkflowLink | A07 구현. instance_url/workflow_id/agent_version_id와 표시/이동용 참조. Flow 정의/비밀을 복제하지 않음 |

AgentVersion 정의는 생성 때부터 불변이며 draft→published→retired로 상태만 변경한다. 설정을 바꾸면 새 버전이다. 발행에는 해당 draft의 성공한 `mode=test` AgentRun이 필요하다. 정상 no_evidence도 성공 시험이지만 답변 적합성은 사람의 인수 대상이다. disabled Agent/retired 버전은 새 실행을 거절하고 기존 기록을 보존한다.

모델 binding은 `{connection_version_id, deployment_id, model_version_id}`다. 검색은 발행·활성 상태의 `knowledge_search_v1` ToolVersion 하나만 허용한다. 접수 시 정의와 연결 snapshot을 저장하고 실제 검색/채팅 직전에도 사용 가능 상태·자원 identity를 확인한다. 연결의 current pointer가 바뀌어도 접수한 버전은 바뀌지 않는다. 자격은 기존 CredentialRef 정책을 따른다.

## 2. 고정 템플릿과 입력

`knowledge_qa_v1` / template_version 1, 실제 LangGraph 1.2.12 StateGraph다.

| 단계 | 처리 |
|---|---|
| validate | 질문과 고정 입력 계약 검증 |
| search | Service callback으로 공통 검색 ToolCall 접수/조회; 부모 실행/request ID 연결 |
| context | 검색 순서를 유지해 전체 청크 단위로 근거 선택, 제외 ID/이유 기록 |
| generate | 준비된 고정 LLM의 공식 Model 채팅 호출. 빈 검색이면 skipped |
| finalize | 답변 인용·출처 검증 후 최종 결과 보고 |

입력은 `{question, filters?}`다. question은 공백만으로 이루어질 수 없고 NUL 없이 UTF-8 8 KiB 이하이며 원문을 임의 trim/변환하지 않는다. filters는 기존 Tool과 같은 source_document_ids/canonical_ids/kinds만 허용한다. 사용자 요청으로 시스템 프롬프트·Tool 목록·모델 URL을 덮어쓰지 못한다. 단일 질문이며 다중 턴 기억·자율 Tool 선택·임의 코드/그래프·자동 재개는 없다.

## 3. Model 채팅·근거·출처

공식 `POST /api/deployments/{id}/chat`의 messages/max_tokens/temperature만 사용한다. `tools/tool_choice/stream`을 보내지 않는다. 배포/모델 버전·ready/runtime-ready·vLLM LLM·served alias를 조회/검사하며 가능한 runtime identity를 결과에 남긴다. 모델 배포·다운로드·재시작은 하지 않는다.

- prompt 최대 UTF-8 4,000바이트, context_max_chars 기본 8,000/허용 256~12,000, max_tokens 기본 512/허용 1~512, temperature 0~1.5.
- 시스템/질문/근거 합계 16,000자·직렬화 JSON 64 KiB 이내. 실제 token context는 Model이 최종 검사한다. Agent에 tokenizer/runtime을 로드하지 않는다.
- Knowledge 최종 순서를 유지하며 전체 청크로 선택한다. 큰 청크를 제외하고 뒤의 작은 청크가 들어갈 수 있지만 재정렬하지 않는다. 하나도 담을 수 없으면 CONTEXT_TOO_LARGE다. 제외 ID와 context_budget 이유를 저장한다.
- Model context/입력 거절은 MODEL_INPUT_REJECTED, finish_reason=length는 answer_truncated=true다. 자동 요약·재시도·다른 모델 fallback은 없다.
- 질문/문서는 JSON 데이터 영역에 둔다. 문서 명령이나 참고문헌을 답변 사실로 오인하지 않도록 지침을 고정한다. 출처는 선택 목록의 `[S1]` 등만 허용하며 없는/잘못된 참조나 무인용 답변은 ANSWER_REFERENCE_INVALID다.
- citations는 `{citation_id, source}`이며 source에 원래 Knowledge hit 전체(본문, 원본 spans, ID, 품질, 점수 등)를 보존한다. Service가 저장된 검색 결과와 독립 비교한다. 인용 식별자 검증이 답변 의미의 정확도를 보증하지는 않는다.

## 4. 구현한 REST API

기존 Agent Bearer 토큰과 엄격한 JSON 검증을 사용한다. 변경 API는 Idempotency-Key를 사용하며 revision 변경에는 expected_revision을 보낸다. 모델 inspect는 읽기 전용이다. 허용하지 않은 필드/null·중복 JSON 키를 거절한다. 목록은 limit/cursor, 기본 20/최대 100이다.

| 메서드·경로 | 역할 |
|---|---|
| POST /api/agents | display_name/description으로 생성 |
| GET /api/agents, GET /api/agents/{id} | 목록·상세/버전 목록 |
| PATCH /api/agents/{id} | 표시 이름·설명·enabled/revision 변경 |
| POST /api/agents/{id}/versions | 고정 정의의 draft 생성 |
| GET /api/agent-versions/{id} | 정의·상태 조회 |
| POST /api/agent-versions/{id}/publish | 성공 시험과 참조 검증 후 발행 |
| POST /api/agent-versions/{id}/retire | 신규 사용 종료 |
| POST /api/agent-models/inspect | action=deployments/resolve. connection_version_id와 cursor 또는 deployment_id로 답변 모델 조회 |
| POST /api/agent-runs | durable 접수 후 202, agent_run_id/status_url와 Location 반환 |
| GET /api/agent-runs | 실행 목록. A08부터 agent_id, agent_version, completed_at, caller_context, error(code/stage 또는 null) 추가. question/ID/state/mode/created_at 유지 |
| GET /api/agent-runs/{id} | 실행 상태·결과·오류·시각 |
| GET /api/agent-runs/{id}/steps | items에 순서가 고정된 5단계 반환 |

발행 버전 실행 예시(실제 버전 ID로 교체):

```json
{
  "agent_version_id": "33333333-3333-4333-8333-333333333333",
  "mode": "run",
  "input": {"question": "이 자료의 연구 범위는 무엇인가요?"},
  "caller_context": {"system": "n8n", "workflow_id": "example", "execution_id": "example", "node_id": "agent-step"}
}
```

mode 생략은 run이다. draft 시험은 test를 명시한다. caller_context는 선택 추적 정보이며 인증/권한 근거가 아니다. UI도 같은 REST를 사용하고 n8n 필드를 요구하지 않는다. A06 AgentRun은 REST로 제공하며 MCP는 기존 Tool 목록/실행을 유지한다.

## 5. 내구성·중복·복구

Agent DB에 AgentRun/정의·연결 snapshot/5단계를 한 트랜잭션으로 저장한 뒤 202를 반환한다. Service의 프로세스당 worker 2개가 claim하고 Python runtime을 호출한다. lease 30초·heartbeat 10초이며 Runtime은 내부 토큰·고정 Service callback을 통해 단계와 검색 호출을 기록한다. Runtime에 DB 자격이나 사용자 지정 callback URL은 없다.

| 상태 | 의미 |
|---|---|
| accepted | 저장된 대기 작업. deadline 내 미실행 작업은 claim 가능 |
| running | 유효 lease를 가진 worker가 실행 |
| succeeded | 검증된 answered 또는 no_evidence 결과 |
| failed | 확정된 검색/모델/입력/시간 초과 등 실패 |
| interrupted | worker 중단·외부 결과 불명확. 전체 그래프 자동 재실행 없음 |

대기 포함 deadline은 기본 300초, 허용 1~600초다. 채팅 호출은 남은 시간과 120초 중 짧은 제한을 사용한다. 만료 accepted는 failed/AGENT_TIMEOUT, 만료 running은 interrupted다. 단계 시작을 먼저 저장하고 외부 호출한다. callback의 단계 순서·lease·deadline과 늦은 최종 보고를 검증한다.

같은 principal·Idempotency-Key·요청 fingerprint는 같은 최초 접수 응답/AgentRun을 반환하며 키가 같고 버전·입력이 다르면 409다. 키는 terminal 기록과 함께 보존한다. 실행 이후 비활성화돼도 기존 동일 키는 기존 접수 결과를 재현한다. 실패를 재실행하려면 명시적으로 새 키/새 Run을 만든다.

검색 자식 ToolCall은 부모 Run/search 단계에 유일하게 연결하고 같은 요청 ID와 고정 snapshot을 사용한다. 내부 callback·최종 결과 보고의 제한된 재전송은 멱등 처리하며 모델 호출이나 그래프를 반복하지 않는다. Tool의 unknown/external_pending은 interrupted로 매핑한다. 타 플랫폼 Operation 상태를 Agent가 수정하지 않는다.

LangGraph는 checkpointer=False다. Service DB의 단계 기록이 내구성 기준이며 체크포인트 기반 중단 재개는 구현하지 않았다. 외부 효과까지 exactly-once라고 주장하지 않는다. 실제 GPU 동시 처리/운영 재시작 검증은 서버 인수로 남아 있다.

## 6. 출력·오류

Run의 result에는 answer, outcome, citations, warnings, answer_truncated, tool_call_id, selected_chunk_ids, excluded_sources, identity, usage가 있다. identity는 template/Tool/Model/Knowledge 참조를 포함한다. 시각과 소요 시간은 Run의 queue_ms/execution_ms, 단계의 duration_ms로 조회한다.

no_evidence는 정상 결과다. 검색 결과 0개면 채팅 없이 반환하고 모델 identity/usage는 비어 있다. 검색 결과가 있어도 모델이 INSUFFICIENT_EVIDENCE를 정상 종료로 반환하면 no_evidence다. 실패는 Run의 error.code/message/stage로 조회하며 검색 상세 원인은 연결된 ToolCall에 보존한다. GET은 실패한 실행도 200으로 반환하므로 HTTP 성공과 업무 성공을 구분한다.

대표 오류는 AGENT_VERSION_UNAVAILABLE, AGENT_TEST_REQUIRED, AGENT_INPUT_INVALID, AGENT_TIMEOUT, AGENT_EXECUTION_INTERRUPTED, AGENT_SEARCH_FAILED, MODEL_NOT_READY, MODEL_IDENTITY_MISMATCH, MODEL_INPUT_REJECTED, MODEL_TIMEOUT, CONTEXT_TOO_LARGE, ANSWER_REFERENCE_INVALID, AGENT_RESULT_INVALID다. 비밀 누락/연결 거절은 기존 Tool 오류 계약을 사용한다.

화면 입력·답변·선택 근거는 Agent 실행 증거로 보관하며 Data/Knowledge 원본을 대체하지 않는다. 일반 로그에 질문·벡터·비밀을 출력하지 않는다. 운영 기록의 보관 정책과 소스에 포함하는 테스트 결과 합계 1 MiB 정책을 구분한다.

## 7. n8n 연결 계약 — A07 구현·사용자 인수 대기

A07 WorkflowLink API의 edit_url은 n8n 주소를 참조한다. A08 우리 UI는 검증한 instance_url/workflow_id로 편집 주소를 만들고 새 탭으로 연다. 원격 PC에서 서버 loopback 주소를 사용할 수 없을 때는 브라우저용 origin만 메모리에서 바꾼다. 로그인은 별도다. iframe/SSO나 n8n UI 내부에 대한 DOM 조작을 첫 단계에 넣지 않는다.

대표 n8n Flow:

1. 질문/필터 입력과 사용할 agent_version_id를 선택한다.
2. 논리 Agent 호출별 invocation key를 생성·보존한다. execution_id만 쓰면 같은 실행의 여러 Agent 노드가 충돌하므로 node/호출 순번 또는 별도 UUID를 구분한다.
3. Agent POST /api/agent-runs를 호출하고 agent_run_id를 저장한다.
4. GET 상태를 제한된 간격/횟수·전체 시간 안에서 조회한다. 202를 최종 답변으로 표시하지 않는다.
5. succeeded이면 answer/citations를 다음 단계에 전달하고, failed/interrupted이면 오류/조회 링크로 분기한다.
6. 네트워크 재시도는 같은 키·버전·입력으로 재조회/재전송한다. 같은 업무를 새 n8n execution으로 재시도할 경우에도 저장한 논리 키를 전달해야 한다.

n8n 자체 재시작·모든 trigger의 재전달까지 자동으로 exactly-once가 된다고 보장하지 않는다. 키를 전달하지 못한 새 수동 실행은 새 작업으로 구분한다. n8n 자체 실행 완료와 내부 AgentRun 완료 상태도 별개로 기록한다.

n8n 사용자가 플랫폼 Tool을 직접 호출할 때도 POST /api/tool-calls와 조회 계약을 사용한다. n8n이 Tool binding이나 외부 API 비밀을 별도 복제하지 않는다. 일반 외부 자동화 노드는 n8n 고유 기능으로 구분한다.

WorkflowLink의 enabled는 연결 표시 상태이며 n8n 게시 상태가 아니다. A08 UI는 연결 숨김/재표시를 지원하고 Flow 삭제는 제공하지 않는다. WorkflowLink 삭제 개념은 n8n 워크플로 삭제와 별개이며 Agent 비활성화는 n8n 워크플로를 수정하지 않는다. 양쪽 상태가 다르면 UI에서 연결 불가 이유를 표시한다. n8n의 workflow ID/version/실행 ID는 제공되는 범위에서 기록하고 미제공 값을 만들어 넣지 않는다.

## 8. 검증·인수

로컬 Go/Python·실제 LangGraph·PGlite SQL/HTTP 대역·실제 Chromium 검증을 마쳤다. 사용자 서버의 Docker/PostgreSQL 16 권한/migration, 실제 준비된 LLM과 선택 자료의 답변 근거 충실성, 완료된 AgentRun 재시작 후 조회는 별도 인수다. [A06 안내](knowledge-qa-a06-20260924.md)에 실행 순서와 요약을 둔다. A07 n8n/UI 대표 Flow 인수는 아직 수행하지 않았다. 기존 학습/OCR/색인 실험을 반복하지 않는다.


## 9. A08 WorkflowLink 목록 확장

GET /api/workflow-links는 기존 문서에 agent_id, agent_display_name, agent_enabled, agent_version, agent_version_state를 추가한다. 고정 AgentVersion과 Agent를 조회하여 표시한 값이며 n8n API를 조회하지 않는다. enabled/revision과 UUID cursor/limit 계약은 유지한다. 단일 연결 GET 계약은 그대로이며 화면에서 버전/Agent 상세를 다시 조회한다. 연결 등록/이름·표시 변경은 기존 멱등/revision API를 사용한다. caller_context로 연결별 Agent 실행을 찾되 인증 증명이나 n8n 전체 실행 상태로 취급하지 않는다.
