# 고정 QA Agent와 질문 연결 — Q06b-2

2026-10-08. GPU 없이 구현·검사하고 서버 코드 적용·관련 검사·로컬 init/status를 완료했다. [상태표](../status.md), [작업계획서](../plans/document-qa-work-plan-20261002.md), [서버 확인](../testing/document-qa-q06b2-server-observed-20261008.json)과 [개발 검사](../testing/document-qa-q06b2-offline-20261008.json)를 따른다.

## 책임과 저장

Agent가 Agent/AgentVersion·AgentRun과 자식 ToolCall을 소유한다. 연결 계층의 [qa_agent.py](../../apps/document_qa/qa_agent.py)는 기존 API만 사용하며 플랫폼 DB·runtime·Flow 정의를 변경하지 않는다. [schema](../../apps/document_qa/qa_agent_schema.py)는 기존 자기 SQLite에 qa_agent_metadata/plans/steps/questions/chat와 보호 trigger를 한 트랜잭션으로 추가한다. Q06b-1 질문 저장·head, upload user_version=1, 호환 library.question_revision=0과 기존 job/key/receipt/Flow는 보존한다.

plan과 chat Connection/모델 조회 snapshot, 접수한 질문과 요청 intent의 식별자는 변경/삭제할 수 없다. 최대 plan 100개·질문 100개, 질문 UTF-8 8192 bytes, 검증된 결과 512KiB다. 초과는 거절하고 조용히 자르거나 오래된 기록을 자동 삭제하지 않는다. 원본/canonical/벡터를 복제하지 않으며 답변과 사용자가 확인할 인용 원문·위치만 필요한 결과로 저장한다.

## Agent 준비·시험·발행

1. Q06a의 검증된 published 검색 Tool plan을 고정한다. 현재 색인 세대·문서 snapshot과 Tool/index/version·embedding 배포/공간/Model/Connection을 확인한다.
2. 선택한 chat Connection 버전·배포·Model 버전과 고정 knowledge_qa_v1 정의를 저장한다. 기존 Agent API의 model inspect로 준비/신원 조회를 수행한다. chat_profile_id=model-platform-chat-v1은 연결 계약의 이름이며 원격 서버가 광고한 serving profile 값이 아니다.
3. 같은 자료함/Agent origin의 논리 Agent를 재사용하고 필요한 새 버전을 생성한다. 기본 prompt, context_max_chars=8000, max_tokens=512, temperature=0, timeout_ms=300000을 명시하며 기존 Agent의 허용 범위 안에서만 변경한다.
4. 별도 추론 opt-in을 가진 시험 AgentRun의 결과를 조회한다. 성공 상태 외에도 고정 입력/키/정의 hash와 자식 ToolCall, 실제 검색 결과를 확인한다.
5. 검증된 시험 후에만 발행 요청을 저장·전송하고 버전/정의/expected revision+1을 다시 조회한다. 발행 응답 유실은 GET으로 확인하며 자동 재발행하지 않는다.

[제한 HTTP](../../apps/document_qa/qa_agent_transport.py)는 정확한 Agent API 경로만 허용한다. 같은 origin, 제한 응답 크기·시간·요청 수, 기존 인증 파일, redirect 거절을 Q06a transport에서 재사용한다. 관리 쓰기와 AgentRun 추론 허용은 별도다. 이번 검사에서 운영 API/모델/GPU는 호출하지 않았다.

## 질문 접수와 결과

client key와 질문을 접수할 때 현재 question_head와 불변 대상·Agent 연결을 검증하고 같은 DB write transaction에서 저장한다. 빈 head나 구조만 맞는 미검증 대상은 접수하지 않는다. 이후 새 문서/색인/head가 생겨도 기존 질문은 당시 target/revision/Tool/Agent/모델을 유지한다. 같은 key와 같은 질문은 기존 기록을 반환하고 다른 질문으로 재사용하면 충돌한다.

AgentRun 전송 전에 intent/key/시도 횟수와 confirmation_required를 commit한다. 응답 유실·강제 종료 후 자동 재전송하지 않는다. 명시 retry는 같은 key/body를 사용하며 receipt가 있으면 기존 Run을 조회한다. 실패/중단은 자동 새 Run으로 바꾸지 않는다. 과거 Run 조회는 당시 버전을 사용하며 새로운 head나 현재 모델 준비 상태로 결과를 재해석하지 않는다.

[결과 검증](../../apps/document_qa/qa_agent_results.py)은 AgentRun의 입력·Go JSON input hash·정의·mode·키, 자식 ToolCall의 parent/step/검색 버전, embedding profile과 전체 문서 snapshot을 확인한다. 답변의 인용 식별자와 원문·자료 ID·source_spans를 실제 ToolCall hit와 대조하고 context/usage/빈 근거·부분 성공을 검사한다. 이미 검증한 terminal 결과는 다른 결과로 교체하지 않는다. 이 검증은 답변의 의미 품질 인수가 아니며 semantic_quality_verified=false다.

## HTTP 연결 상태

[make_server](../../apps/document_qa/api.py)에 qa_agent와 agent_factory를 명시 주입할 때 아래 경로를 사용한다. 기존 Bearer 인증과 Host/Origin 검증을 유지한다.

| 경로 | 동작 |
|---|---|
| POST /api/questions | Idempotency-Key와 question만 받아 로컬 접수, 202. 자동 AgentRun 전송 없음 |
| GET /api/questions, GET /api/questions/{id} | 저장된 목록/상태/검증된 결과. 원격 호출 없음 |
| GET /api/question-requests/{key} | 응답 유실 후 원래 client key로 기록 조회 |
| POST /api/questions/{id}/dispatch, /retry | 빈 JSON과 사전에 허용한 factory로 전송/명시 재확인 |
| POST /api/questions/{id}/observe | 빈 JSON과 factory로 기존 AgentRun/ToolCall 조회 |
| GET /api/qa-agent | 로컬 저장 상태 |

Q06b-2 당시 CLI serve는 adapter/factory를 자동 연결하지 않았다. Q06b-3의 [head CAS/정책](question-head.md)에서는 이미 초기화한 전환 저장을 읽어 기본 serve에 연결하며 실제 head 전환과 질문 추론은 별도 명시 옵션이 필요하다. 이번 코드 적용·로컬 init/status에는 기존 API/서비스 재실행이 필요 없다. 질문 가능 상태를 true로 바꾸거나 fixture SQL을 서버에서 실행하지 않는다.

## 서버 로컬 확인 명령

아래 명령은 Q06b-1 migration까지 완료한 기존 sh_test 환경과 자기 상태에서 통과했다. 절차 참조용이며 문서 패치 적용 때문에 검사/init/status나 앞 단계 migration·성공한 업로드/Flow callback을 반복하지 않는다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_qa_agent.py' -q
python3 -B -m apps.document_qa.qa_agent init
python3 -B -m apps.document_qa.qa_agent status
```

init/status는 운영 API·AgentRun·서비스·추론/GPU를 요청하지 않는다. question_ready=false, head_activation_implemented=false, production_agent_verified=false를 유지하며 다음 개발은 Q06b_3_question_head_CAS_activation이다. 기존 question_store의 이전 단계 status는 보존하고 현재 단계 상태는 qa_agent status로 확인한다.

패키지 원복은 코드/문서만 되돌리며 추가 DB/질문 이력·키를 삭제하지 않는다. 실행 중인 새 프로세스의 구버전 복귀·미완료 요청 처리는 이후 운영 인수 범위다. 실제 GPU 색인·Tool/Agent 시험/발행·head 전환·질문 품질은 후속 인수다.
