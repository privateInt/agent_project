# Q06a 검색 Tool 연결

2026-10-08. Agent의 기존 `knowledge_search_v1`을 QA 자료함에 연결하는 adapter다. 서버 코드 일치·관련 검사·로컬 init/status는 확인했다. 완료/검증 범위는 [현재 상태](../status.md), 다음 저장 migration은 [작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.

## 책임과 저장 계약

Agent가 Connection·Tool·ToolVersion·ToolCall을 소유한다. 이 연결 계층은 기존 자기 SQLite에 `search_tool_metadata`, `search_tool_plans`, `search_tool_steps` 세 테이블만 추가한다. 업로드 schema의 `user_version=1`과 `question_revision=0`, 기존 작업·receipt·Flow 설정·키를 유지하며 새 credential은 만들지 않는다. 질문 head 저장/API·n8n 연결·AgentRun·사용자 화면은 이번 범위 밖이다.

`SearchToolStore.candidate()`는 이미 활성화된 Q05 색인 head와 해당 작업의 snapshot·build intent·receipt·활성화 입력을 검증한다. generation 0·미완료/진행 중 색인은 후보가 아니다. `plan(selection)`은 그 후보의 문서 전체·모델/profile·색인 버전과 두 Connection 버전을 고정한다. 같은 generation/선택은 같은 plan을 재사용하며 선택 변경은 충돌이다. 색인 generation은 질문 revision과 다르다.

selection은 adapter 호출자가 공급하는 고정 참조다. `agent_origin`, `knowledge_connection`과 `model_connection`의 `version_id`/`base_url`, `top_k` 1~50, `candidate_k` top_k~100, `timeout_ms` 1,000~240,000을 받는다. 기존 ConnectionVersion을 조회해 재사용하며 Connection 생성·인증 변경은 하지 않는다. 운영 Connection 후보의 자동 발견/선택과 사용자 화면은 아직 구현하지 않았고, 일반 사용자에게 이 ID를 입력시키는 흐름으로 완성 처리하지 않는다.

호스트와 Agent 내부의 API 주소는 다를 수 있다. ConnectionVersion의 주소/인증 참조를 고정하고, Agent `knowledge/inspect`의 resolve를 통해 동일한 index/version·embedding 공간/배포를 확인한다. resolve는 하위 GET만 사용하는 준비 조회로서 추론은 하지 않지만 모델 profile이 ready여야 한다. 현재 GPU 보류 환경에서는 운영 prepare도 실행하지 않는다.

## 호출과 재실행

| 함수 | 처리 | 확인 기준 |
|---|---|---|
| `prepare(plan_id, gateway)` | Connection 조회/resolve → 논리 Tool 재사용/생성 → 고정 버전 draft 생성 → GET | 실제 ID·schema·binding·definition hash. 같은 자료함의 후속 색인은 같은 논리 Tool의 새 버전 |
| `test(plan_id, gateway, query)` | 고정 draft의 mode=test 호출 접수 → GET | 접수 202는 waiting. 호출 ID·invocation key·질문 hash·definition hash·result.data/metadata·model/profile·문서 참조 검증 |
| `publish(plan_id, gateway)` | 성공 시험을 다시 조회 → expected_revision 발행 → GET | published 상태·revision 증가·동일한 definition을 확인한 뒤 tool_binding_verified |
| `observe(plan_id, gateway)` | 저장된 ID로 GET | POST·추론 없이 확인. 발행 응답 유실도 고정 버전 GET으로 확인 |

관리/시험 POST 전에 원래 입력·hash·멱등 키·시도를 저장한다. 생성/시험 접수 응답이 유실됐으면 자동 POST를 반복하지 않는다. 명시 `retry=True`만 같은 body/key로 재전송하며 새로운 키/ToolCall을 만들지 않는다. 버전 생성의 expected_revision도 최초 입력을 유지하므로 충돌을 임의로 재기준화하지 않는다. receipt가 있으면 GET으로 확인하고 POST하지 않는다. 프로세스 중단 뒤에도 이 원칙을 유지한다.

동시 실행은 QA 색인 lane과 별도 Tool lane으로 직렬화한다. 원격 API 대기 중 SQLite 트랜잭션을 열어 두지 않는다. 이전 색인/ToolVersion은 수정하지 않는다. failed/interrupted/unknown 시험은 terminal 실패로 보존한다. 실패한 시험의 새 실행·원인 개선은 별도 후속 계약이며 기존 성공으로 덮지 않는다.

결과 wrapper·연결/모델/색인 신원·개수·출처를 검증하지만 정답 의미 품질을 대신 평가하지 않는다. 빈 검색 결과도 정상 반환으로 보존한다. QA 저장에는 본문·벡터·검색 hit text를 복제하지 않고 ID와 작은 검증 요약만 둔다. 질문 원문은 같은 요청 재현을 위해 제한된 자기 DB의 test intent에 저장하며 상태 출력에는 넣지 않는다.

## HTTP와 실행 허용

`AgentToolGateway`는 기존 origin/token_file 참조를 재사용한다. 각 함수 호출에는 새 Gateway/budget를 공급한다. 관리 POST에는 `execute=True`, Tool 시험에는 `allow_inference=True`도 필요하다. 둘 다 기본 false다. 시험 허용 없이 ToolCall을 접수하지 않으며, 질문 실행 mode=run과 AgentRun·모델 기동·색인 변경 API는 transport가 허용하지 않는다.

허용 경로는 특정 ConnectionVersion/Tool/ToolVersion/ToolCall GET, read-only resolve POST, Tool/버전 생성·시험·발행 POST뿐이다. 16요청/90초 budget·요청별15초 timeout·요청64KiB·응답2MiB를 적용한다. redirect/proxy를 따르지 않고 backend 원시 오류/credential은 오류 출력에 넣지 않는다. resolve의 Idempotency-Key는 Gateway 공통 POST header 계약을 위한 고정 키이며 관리 변경 intent가 아니다.

`tool_binding_verified`는 해당 adapter가 조회한 자산의 상태다. 이번 오프라인 검사와 CLI status는 운영 인수로 표시하지 않아 `production_tool_verified=false`다. Tool 발행만으로 질문은 준비되지 않으며 항상 `question_ready=false`를 유지한다. Q06b 저장/Agent/CAS와 Q05c 실제 색인·Q06 운영 인수를 거쳐야 질문 준비 상태를 전환한다.

## 서버 로컬 확인 명령

Q05a/Q05b까지 초기화된 기존 자기 저장소를 사용한다. CLI는 로컬 `init`, `status`, `candidate`만 제공한다. 아래 관련 검사·init/status는 사용자 서버에서 확인했으며 문서 변경 때문에 반복하지 않는다. 운영 Tool 인수는 별도 단계다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_search_tool.py' -q
python3 -B -m apps.document_qa.search_tool init
python3 -B -m apps.document_qa.search_tool status
```

초기 `plan_count=0`, `question_ready=false`가 정상이다. 현재 보류 색인의 `candidate`는 `SEARCH_TOOL_ACTIVE_INDEX_REQUIRED`로 종료하며 이를 색인 재실행 사유로 취급하지 않는다. 운영 selection 생성·Tool 등록/시험/발행·추론·QA 프로세스 재실행은 위 명령에 없다. 기존 QA API와 Flow에 이번 모듈을 자동 연결하지 않았으므로 서비스 재시작이 필요하지 않다.

코드 원복은 전달 패키지의 `apply_patch.py --rollback`으로 수행한다. 추가한 로컬 테이블/처리 기록과 원격 자산을 삭제하지 않으며 구버전 코드는 추가 테이블을 무시한다. Q06b의 질문 head migration은 이와 별개로 인수한다. 서버 확인 범위와 최초 패키지 차단 이력은 [Q06a 서버 근거](../testing/document-qa-q06a-server-observed-20261008.json), 개발 검사 범위와 제한은 [Q06a 오프라인 근거](../testing/document-qa-q06a-offline-20261008.json)에 기록한다.
