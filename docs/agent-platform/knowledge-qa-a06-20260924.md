# A06 문서 질의응답 Agent — 적용·사용·검증

**서버 후속 확인:** [2026-09-24 사용자 로그](../history/agent-platform-202609.md#a06-server-check-20260924)로 Docker 빌드·5개 Healthy·native DB/Agent 통합 검사가 통과했다. 아래 구현 당시의 미확인 상태와 구분한다. 사용자 요청으로 화면·실제 LLM 답변/출처·재시작 조회는 보류하고 [A07 기능](n8n-a07-20260924.md)을 먼저 진행한다.

2026-09-24. 구현·로컬 검증 완료, 사용자 서버 인수 대기. 빌드 `a06.1`, 계약 `agent.platform.v1/a06`, DB schema `4`.

질문 → 기존 자연어 검색 Tool → 근거 선택 → 답변 모델 → 출처가 있는 답변을 제공한다. 검색 Tool과 답변 모델을 한 번 선택해 Agent로 저장하면 사용자는 질문만 입력한다. Agent/버전/실행/단계 기록은 Agent Platform이 소유한다. Model·Data·Knowledge 실행 코드는 변경하지 않았다. n8n 연결은 다음 A07 범위다.

## 1. 서버 적용

이미 A05를 적용한 프로젝트에서 아래 패치를 적용한다. 전체 소스는 별도 보관용이며 운영 폴더 위에 통째로 덮어쓰지 않는다. 적용기는 모든 대상 파일의 해시를 먼저 검사하고, 변경분 백업 후 파일을 교체한다. 로컬 수정 충돌이 있으면 쓰기 전에 멈춘다. `.env`·비밀·DB 볼륨은 패치 대상이 아니다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_a06_20260924.tar.gz
python3 agent_platform_a06_20260924/apply_patch.py \
  --project-root ~/workspace/agent_project --apply

cd ~/workspace/agent_project/agent_platform
python3 ops/preflight.py --skip-port-check
docker build --target tests -t agent-platform-go-tests:a06.1 .
docker build -f runtime/Dockerfile \
  --target tests -t agent-platform-python-tests:a06.1 .
docker compose up -d --build --wait
python3 ops/smoke_tools.py \
  --call-id 8e3b3160-8e29-49d8-96d2-e0540e67682e
docker compose --profile checks run --build --rm agent-db-checks
)
```

Service 기동 시 additive migration `004_agents.sql`이 적용된다. 기존 ToolCall은 유지되고 AgentRun/AgentVersion/단계 테이블과 부모 실행 참조가 추가된다. 마지막 smoke는 기존 A03 실행을 **조회만** 하며 원래 request ID `b611f3d7-8d9f-4dc1-9110-04496f5278ea`를 확인한다. DB checks는 별도 테스트 실행 lane과 HTTP 대역으로 검사하며 실제 사용자 지식이나 GPU를 호출하지 않는다. 테스트 정의/기록은 남고 테스트 Tool은 비활성화한다.

패치 백업은 소스 파일 백업이다. schema 4 적용 뒤 A05 이미지만 되돌리는 것은 지원하지 않는다. DB를 포함한 복구가 필요하면 해당 소스와 일치하는 DB 백업을 사용하는 기존 운영 복구 절차를 따른다.

## 2. 화면에서 처음 사용하는 순서

주소는 `http://<서버IP>:16100/console`이다. 새로고침하고 기존 Agent API 토큰으로 연결한다. 토큰은 브라우저 메모리에만 두므로 새로고침 후 다시 입력한다.

1. **도구 목록**에서 A05에 만든 자연어 지식 검색 도구를 연다. 성공한 시험 기록이 있고 아직 초안이면 **사용 시작**으로 발행한다. 직접 등록한 벡터 검색 HTTP Tool은 Agent의 검색 도구 목록에 나오지 않는다. Agent는 자연어를 받는 `knowledge_search_v1`의 발행 버전을 사용한다.
2. **Agent → Agent 만들기**를 누르고 이름을 입력한다. 예: `문서 답변 도우미`.
3. **검색 도구**를 선택한다. 해당 도구에 저장된 지식·검색 버전·임베딩 연결을 그대로 사용한다.
4. **답변 모델 불러오기**를 누르고 준비된 LLM을 선택한다. 기본 Model 서비스 연결은 검색 도구의 고정된 연결을 사용한다. 별도 Model 연결을 쓸 때만 상세 설정을 바꾼다. 임베딩·reranker 배포는 답변 모델이 아니다. 목록이 비어 있으면 기존 Model Platform에서 LLM 준비 상태를 확인한다. 이 화면은 모델을 배포하거나 시작하지 않는다.
5. **저장 후 질문하기**를 누르고 선택한 자료로 답할 수 있는 질문을 입력한다. 기존 schema·UUID·벡터를 직접 작성할 필요가 없다.
6. **시험 질문하기**을 실행하고 답변의 `[S1]` 등 인용, 출처 본문·쪽수·품질 정보를 비교한다. 정상 실행이라고 답변 의미까지 검증된 것은 아니다.
7. 확인을 마치면 **사용 시작**을 누른다. 이후 이 Agent를 열어 질문하면 된다. 설정 변경은 **새 버전 만들기**로 저장하고 다시 시험한다.

시험과 일반 질문 모두 실제 검색·모델 호출이다. 채팅 기억을 이어가는 다중 턴 대화는 아니며 각 질문은 독립 실행이다. 질문의 최대 크기는 UTF-8 8 KiB다. 자료 필터·프롬프트·근거 길이·생성 길이는 상세 설정에 있다.

사용자가 앞서 보낸 A05 화면은 검색 결과 5개와 가죽 생산/LCA 문헌 일부였다. 당시 질문·선택 지식은 전달되지 않았다. 현재 선택한 자료의 내용에 맞는 질문으로 확인하고, 참고문헌 조각이 질문에 답할 근거인지도 함께 본다.

## 3. 결과와 실패를 읽는 방법

| 표시 | 의미·다음 행동 |
|---|---|
| 답변 + 출처 | 선택한 검색 청크 안의 인용 식별자를 검증한 결과. 원문과 답변의 의미를 비교한다. |
| 근거 부족 | 검색 결과가 없거나 모델이 자료로 답할 수 없다고 응답했다. 전자는 채팅 호출을 생략한다. 질문·선택 지식을 확인한다. |
| 답변이 잘렸을 수 있음 | Model `finish_reason=length`. 현재 생성 한도는 최대 512토큰이다. 더 좁은 질문이나 짧은 답변 지침을 사용한다. |
| `MODEL_NOT_READY` / `MODEL_IDENTITY_MISMATCH` | 선택한 배포 준비 상태/버전 확인. 다른 배포로 자동 전환하지 않는다. |
| `MODEL_INPUT_REJECTED` / `CONTEXT_TOO_LARGE` | 근거 예산·질문 길이와 Model context 한도 확인. 근거 예산 변경은 새 Agent 버전으로 시험한다. |
| `ANSWER_REFERENCE_INVALID` | 모델이 출처를 생략하거나 제공되지 않은 식별자를 인용했다. 답변을 성공으로 저장하거나 출처를 임의 보완하지 않는다. |
| `AGENT_SEARCH_FAILED` | 검색 ToolCall을 열어 원래 검색/임베딩 오류를 확인한다. |
| `interrupted` | 작업자 중단 등으로 안전한 완료를 확인할 수 없다. 자동으로 전체 그래프를 다시 실행하지 않는다. |

질문 접수 응답이 끊기면 **이 질문 요청 다시 확인**을 누른다. 같은 질문·멱등 키로 기존 실행을 복구한다. 실행 ID를 받은 뒤에는 **이 실행 결과 다시 확인**을 사용한다. 페이지를 새로고침한 뒤에도 토큰 연결 → Agent → 이전 질문에서 접수된 기록을 조회할 수 있다. 실패한 실행은 같은 키로 재전송해도 다시 실행하지 않으며 새 질문 실행은 새 ID를 만든다.

## 4. 서버의 REST·재시작 확인

화면에서 Agent 시험·발행을 끝낸 뒤 필요할 때 한 번 실행한다. Agent 이름과 질문은 실제 설정/자료에 맞게 바꾼다. 아래는 별도 실제 모델 호출을 발생시킨다.

```bash
python3 ops/smoke_agents.py \
  --agent-name '문서 답변 도우미' \
  --question '이 자료에서 설명하는 연구 범위는 무엇인가요?' \
  --state-file ~/workspace/a06-agent-smoke.json
```

정상 결과는 `result: passed`, `stage: A06`, `mode: rest_replay`다. state 파일에는 고정한 AgentVersion/요청 키/입력 지문만 저장하고 토큰·질문·답변은 저장하지 않는다. 응답 유실 시 같은 인자와 파일로 다시 실행한다. 다른 질문에 같은 파일을 쓰지 않는다. 화면 시험 기록만 재확인하려면 다음 lookup 명령을 곧바로 사용해 추가 모델 호출을 생략할 수 있다.

완료된 실행의 ID와 원래 request ID를 보관한 뒤 재시작·조회한다. 아래 두 자리표시자는 실제 ID로 교체한다.

```bash
(
set -e
docker compose restart \
  agent-postgres agent-service agent-runtime agent-mcp agent-gateway
docker compose up -d --no-build --wait
python3 ops/smoke_agents.py \
  --run-id '<완료된 AgentRun ID>' \
  --request-id '<원래 request ID>'
)
```

`mode: lookup`은 새 추론 없이 AgentRun·AgentVersion·원래 요청 ID·검색 ToolCall 부모 관계·5단계 기록을 조회한다. 답변·출처 보존도 화면의 이전 질문에서 확인한다. 실행 중 재시작을 정상 완료 보존 검사와 섞지 않는다.

## 5. 구현 계약과 운영 범위

- 실제 LangGraph `1.2.12`의 StateGraph를 사용한다. `validate → search → context → generate → finalize`, 빈 검색이면 generate를 skipped로 남긴다. 고정 템플릿 `knowledge_qa_v1`, version 1이다. [공식 StateGraph 계약](https://reference.langchain.com/python/langgraph/graph/state/StateGraph)을 따른다.
- `compile(checkpointer=False)`. 내구성 있는 AgentRun·단계 기록의 기준은 Service DB이며 Runtime에 DB 자격을 부여하지 않는다. 외부 tracing은 비활성화한다. 별도 LangGraph checkpoint 기반 재개는 구현하지 않았다.
- 실행 접수 시 AgentVersion·발행된 검색 ToolVersion·모델/검색 ConnectionVersion을 snapshot으로 고정한다. 내부 callback은 고정 Service 주소·내부 토큰·lease로만 허용하며 사용자 입력으로 callback URL을 지정하지 않는다.
- Service 프로세스당 Agent worker 2개, lease 30초·heartbeat 10초다. 대기 포함 전체 deadline 기본 300초/상한 600초, 채팅 HTTP는 남은 시간과 120초 중 짧은 제한을 쓴다. 실제 GPU 동시 처리 성능은 별도 확인 대상이다.
- 같은 호출자·멱등 키·버전·입력은 같은 AgentRun이다. 접수는 202, 완료 여부는 GET state/result로 확인한다. 결과 보고만 재시도하고 모델/전체 그래프를 자동 재실행하지 않는다. expired running은 interrupted, expired accepted는 timeout이다.
- 단계 시작을 저장한 뒤 외부 API를 호출한다. 검색은 기존 ToolWorker의 공통 ToolCall을 사용하고 부모 AgentRun·search 단계·request ID를 연결한다. 과거 ToolCall은 부모 필드가 없어도 기존 계약을 유지한다.
- 답변 모델은 준비된 vLLM LLM의 deployment ID/model_version_id를 고정한다. 공식 `POST /api/deployments/{id}/chat`만 사용하며 배포 생성/재시작/파일·Docker·GPU 직접 접근은 없다.
- 검색 순서를 유지한 완전한 청크만 근거로 선택한다. 예산을 넘는 청크는 ID/이유를 남겨 제외한다. 기본 근거 8,000자, 설정 256~12,000자, 시스템/질문 포함 메시지 합계 16,000자·JSON 64 KiB, max_tokens 1~512를 적용한다. 실제 token context는 Model이 최종 판단하며 자동 축약·다른 모델 재시도는 없다.
- 답변 인용은 선택 근거의 `[S숫자]`만 허용한다. citations의 source는 Knowledge hit 전체를 보존하므로 출처·품질·점수·향후 필드가 유지된다. API의 출처 식별자 검증은 의미적 정답 보증이 아니다.
- draft는 `mode=test`, published는 일반 `mode=run`으로 실행한다. 성공 시험이 있어야 발행된다. no_evidence도 정상 실행이므로 발행 가능하지만 자료/질문의 적합성은 사용자가 확인한다.

상세 API와 오류는 [Agent 계약](api-agents.md), 다음 작업은 [작업계획](work-plan.md)을 따른다. 서비스별 API 묶음 관리·범용 schema 자동완성·n8n·임의 그래프·다중 턴 기억은 이번 완료 범위에 포함하지 않는다.

## 6. 검증 근거와 남은 인수

| 구분 | 결과·범위 |
|---|---|
| Go | 전체 패키지 빌드/단위 검사 통과. 실제 DB/Runtime 없는 실행에서는 해당 통합 검사가 skip된다. |
| Python | Runtime 42개, ops 15개 통과. 실제 LangGraph 실행, 빈 검색, 인용 오류, 예산, 모델 거절/identity, lease 검사 포함. |
| 기존 화면 | Console 27·직접 등록 21·Knowledge 11개 DOM/API 대역 검사 통과. |
| SQL·wire | PGlite PostgreSQL 18.3 WASM + 실제 Go↔Python gRPC + HTTP 대역으로 Tool/OpenAPI/Knowledge/Agent 전체 통과. native PostgreSQL 권한/Foundation은 별도이며 이 환경에서 skip. |
| 실제 Chromium | 등록 응답 유실 후 동일 버전 복구, 질문 접수 응답 유실 후 동일 실행 복구, 답변/출처/5단계, 발행·일반 질문, 새로고침 후 이전 질문, PC/390px 화면 통과. Model/Knowledge는 HTTP 대역. |
| 멱등·복구 | 같은 키 재전송·다른 입력 충돌·연결 포인터 변경·단계 순서·늦은 결과 차단·보고 재전송·worker lease 만료·대기 timeout 통과. CLI 동일 state 파일 재실행 시 추가 채팅 없음. |
| 실제 사용자 서버 | Docker 빌드·PostgreSQL 16 migration/권한·준비된 GPU LLM/실제 지식 답변·발행·재시작 후 조회는 아직 미확인. |

전체 SQL 회귀 검사에서 테스트 종료 시 DB 정리 timeout이 한 번 발생했다. 취소된 요청 context와 분리한 3초 rollback으로 보강한 뒤 같은 전체 SQL/Service 검사가 통과했다. 첫 실패는 검증 요약에 남겼다. 브라우저에서 발견한 모델 선택 후 저장 버튼 활성화와 응답 유실 복구도 수정·재검증했다.

압축된 기계 판독 요약은 [a06-local-20260924.json](../../src/platform/agent/tests/results/a06-local-20260924.json)이다. A05의 사용자 UI 시험 성공은 [별도 근거](../history/agent-platform-202609.md#a05-server-ui-check-20260923)로 유지하며 A04/A05 전체 서버 인수로 확대하지 않는다. 이번 전달물은 소스이며 운영 DB/볼륨 백업이 아니다.
