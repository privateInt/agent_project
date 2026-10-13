# 검증된 질문 head의 CAS 전환 — Q06b-3

2026-10-08. GPU 없이 구현하고 서버 코드 적용·관련47개 검사(14.385초)·로컬 init/status까지 확인했다. 실제 운영 전환과 질문/품질 인수는 남았다. [상태표](../status.md), [계획서](../plans/document-qa-work-plan-20261002.md), [서버 근거](../testing/document-qa-q06b3-server-observed-20261008.json), [개발 검사 근거](../testing/document-qa-q06b3-offline-20261008.json)를 따른다.

## 저장과 전환

[question_head.py](../../apps/document_qa/question_head.py)는 Q06b-1 question_head/불변 target과 Q06b-2 Agent plan을 재사용한다. [schema](../../apps/document_qa/question_head_schema.py)는 qa_head_metadata/qa_head_activations 두 table과 receipt 변경/삭제 금지 trigger 두 개를 한 트랜잭션으로 추가한다. 기존 schema·upload user_version=1·library.question_revision=0·job/key/receipt/Flow는 유지한다. init는 head를 만들거나 바꾸지 않고 이미 있는 미검증 양수 head·부분/다른 schema를 초기화로 덮어쓰지 않는다.

전환 입력은 client key, plan_id, expected_revision뿐이다. caller가 제공한 target/verified/ready flag는 받지 않는다. [재검증](../../apps/document_qa/question_head_results.py)은 현재 자기 색인 세대·전체 문서 snapshot·완료/활성화 receipt와 운영 index/version·입력/청커/embedding profile을 대조한다. 이어 고정 Connection/인증 참조 revision·Tool/Agent 정의·발행 revision·ToolCall/AgentRun과 자식 검색/원문 인용을 다시 읽는다. chat model inspect는 신원 조회이며 추론이 아니다. 이미 검증한 시험 결과 hash가 바뀌면 거절한다. 마지막 index/Tool/Agent 조회로 검사 중 변경도 확인한다.

전환은 Agent/Tool/색인/질문 lane을 잠근다. 원격 검증 중에는 DB write transaction을 열지 않는다. 검증 전 로컬 근거 snapshot을 기록하고 commit transaction에서 재대조한다. 순수 [switch_head](../../apps/document_qa/contracts.py) 계약과 조건부 UPDATE로 expected_revision+1을 확인하면서 target 삽입·head 갱신·불변 전환 receipt를 한 번에 commit한다. 실패나 commit 전 프로세스 종료는 세 변경을 모두 취소하고 기존 정상 head를 유지한다.

같은 key/plan/revision은 원래 receipt를 반환하고 다른 요청으로 key를 재사용하면 충돌한다. 이후 head가 바뀌어도 원래 요청의 receipt를 유지한다. 응답 유실 후 GET으로 조회하거나 같은 입력/키를 재접수하면 원격 재검증/중복 전환 없이 복구한다. 다른 key로 같은 현재 대상의 revision만 올리는 요청도 거절한다. 전환은 최대 100개, target 저장의 기존 최대 100개 한도를 공유한다. 오래된 이력은 자동 삭제하지 않는다.

## 질문과 API 정책

새 질문은 현재 head와 전환 receipt를 같은 SQLite write transaction에서 확인하고 Q06b-2 질문 기록에 고정한다. 이전 client key/질문은 원래 target/revision으로 복구하며, 새 문서/색인/Agent/head가 생겨도 과거 요청·답변·실패 이력을 교체하지 않는다. 답변 의미 품질은 별도 인수이며 semantic_quality_verified=false다.

기본 serve는 이미 초기화한 Q06b-3 저장의 adapter/factory를 읽어 질문 접수/로컬 조회를 연결한다. 미초기화 저장은 자동 확장하지 않고 새 경로를 비활성으로 둔다. 잘못된 schema/receipt는 fail closed한다. 기존 인증·Host/Origin·요청 크기/동시 제한은 유지한다.

| 인증된 경로 | 동작 |
|---|---|
| GET /api/question-head, /api/question-policy | 로컬 head/receipt·실행 정책 조회. 원격 호출 없음 |
| GET /api/question-head-requests/{key} | 당시 전환 receipt 조회. 현재 head와 구분 |
| POST /api/question-head/activate | Idempotency-Key와 plan_id/expected_revision. 별도 전환 허용 및 읽기 전용 재검증 필요 |
| POST /api/questions, GET 질문/요청 경로 | Q06b-2 접수/원래 요청 복구·이력/결과 조회. 자동 추론 없음 |
| POST 질문 dispatch/retry | 별도 질문 추론 허용 필요. 기존 intent/receipt 복구 규칙 유지 |
| POST 질문 observe | 기존 Run/자식 ToolCall만 조회. 새 Run/추론 생성 없음 |

serve의 --allow-question-head-activation은 자기 head의 운영 CAS만 허용하고 Agent/Knowledge transport의 쓰기/추론을 켜지 않는다. --allow-question-inference는 질문 AgentRun만 허용하며 --allow-indexing-inference와 독립적이다. 세 옵션은 기본 false다. 활성 head와 질문 추론 허용이 함께 있을 때 /api/question-policy와 자료함의 question_ready가 true다. 로컬 CLI와 구단계 질문 응답의 question_ready=false는 현재 실행 허용을 의미하지 않으므로 정책 경로로 확인한다. production_question_verified/semantic_quality_verified는 단순 설정으로 true가 되지 않는다.

## 완료한 서버 로컬 확인

기존 sh_test와 자기 상태에서 아래 명령의 통과를 확인했다. 문서 패치 적용 때문에 검사/init/status나 이전 init/migrate·성공 업로드·Flow callback·API/서비스 시작을 반복하지 않는다.

```bash
python3 -B -m unittest discover -s apps/document_qa/tests -p 'test_question_head.py' -q
python3 -B -m apps.document_qa.question_head init
python3 -B -m apps.document_qa.question_head status
```

서버 init/status는 question_head_schema=1·head_activation_implemented=true를 확인했으며 head revision=0·대상 없음·target/activation/plan/question=0, head_active=false·question_ready=false다. 운영 API 요청=0·추론/GPU 요청=false를 보고했다. 실제 head 전환을 수행했다는 뜻은 아니다. next_step은 GPU 사용 가능 시 Q05c_1_first_document_live_indexing_when_GPU_available이며 자동 실행 지시가 아니다. 전환/추론 옵션을 추가해 서버를 실행하거나 fixture SQL로 head를 바꾸는 명령은 이번 로컬 확인에 없다.

실제 GPU 색인/검색→Tool/Agent 시험·발행/readback→CAS→질문/품질 인수는 후속이다. 검증과 commit은 서로 다른 플랫폼을 포함한 분산 트랜잭션이 아니다. 외부 관리자가 검사 직후 자산을 변경할 가능성은 남으며 이후 dispatch/조회도 고정 자산과 결과를 검증한다. SQLite/file lock은 단일 Linux 호스트·전용 볼륨 계약이다. 여러 replica/운영 rollback·재부팅·Kubernetes는 별도 인수다.

패키지 --rollback은 코드/문서만 되돌리고 추가 DB/전환/질문 이력·키를 삭제하지 않는다. 구버전 코드는 추가 qa_head_* schema를 무시할 수 있으나 운영 전환을 취소하는 명령이 아니므로 실행 중인 프로세스의 복귀는 별도 절차가 필요하다.
