# 문서 질의응답 서비스 작업계획서

갱신: 2026-10-12. 사용자가 문서를 업로드하고 질문하면 자료를 검색해 답변과 출처를 받는 서비스를 목표로 한다. 첫 TXT의 실제 GPU 색인/Tool/Agent smoke·발행·head revision1과 PC 접수는 확인했고 현재 인용 누락 해결이 첫 작업이다. 최신 완료/실패 근거는 [상태표](../status.md)에 둔다. Q01~Q08 최초 로컬 인수는 당시 근거로 보존한다. 이후 확장/CI 우선순위는 [확장 계획](platform-expansion-work-plan-20261012.md)을 따른다.

## 제품 범위와 내부 책임

첫 범위는 하나의 자료함, TXT·Markdown·텍스트 PDF다. 스캔 PDF는 OCR 준비·내용 품질을 별도로 확인한 뒤 지원한다. Data는 원본/canonical, Knowledge는 청킹/색인, Model은 추론, Agent는 Connection/Tool/고정 QA 버전·실행을 소유한다. n8n은 상위 준비 workflow, LangGraph는 Agent 내부 `knowledge_qa_v1` 실행기다. 연결 계층은 자료함/job·단계 intent/receipt·질문 대상 참조를 보관하며 타 플랫폼 DB/스토리지를 직접 쓰지 않는다.

일반 사용자에게 모델·색인·Tool ID나 n8n 편집을 요구하지 않는다. 기존 설정·키·배포를 재사용하고 운영 기본값만 한 번 준비한다. 필수 모델이 없거나 중지됐으면 준비 미완료로 표시한다. 10월 12일 사용자가 GPU와 PC 화면 확인 가능을 알렸고 문서 QA의 실제 추론/첫 head 전환을 수행했다. 해당 사용 허용과 Ops 백업·재부팅·Kubernetes 보류는 별개다.

| 한도/기본값 | 계획에 유지할 계약 |
|---|---|
| 원본 | 파일당 32MiB, 자료함 활성 문서 최대 20개. 초과는 접수 전에 안내 |
| 색인 입력 | canonical 최대 20개·정제 본문 합계 32MiB, 전체 canonical 응답의 별도 byte 한도 유지 |
| 청커 | 초기 `structure_v1` 1000/100 프리셋. Model 입력 한도와 실제 청크 token 수 확인; 품질 우위는 아직 미인수 |
| 모델 | embedding/chat 필수, OCR/reranker 선택. 모델 버전·embedding 공간·Connection 버전 고정 |
| 완료 표시 | ready 색인만으로 질문 가능 아님. Tool/Agent 시험·발행·바인딩 readback과 질문 대상 전환까지 확인 |

## 다음 작업의 순서

| 순서 | 작은 작업 | 진행 방법과 완료선 |
|---|---|---|
| 첫 작업 / F0a | 첫 문서 인용 누락 해결 | 두 실패 Run의 저장 진단·prompt/payload/model을 읽고 변화 하나의 비교. 코드/부서 질문·실제 원문 인용·근거 없는 질문을 확인. 검증 후보만 시험/발행/readback·CAS 전환 |
| F10a | CI 기준선 | 기존 CPU/계약/화면 검사를 지원 환경에서 실행. Node12 skip을 필수 JS 검증 통과로 취급하지 않음 |
| Q05c-2 | 두 번째 문서 추가 | 기존 전체 활성 자료 snapshot으로 새 색인 버전·검색·Tool/Agent/head 준비. 두 문서의 고유 내용·과거 질문 보존 |
| Q09a | 자료 추가/제외 | 자료함 변경 직렬화·새 revision·마지막 제외. 제외 자료는 새 검색/답변에서 빠지고 과거 질문/자산은 보존 |
| Q09b | 장애·중복·재개 | 응답 유실·QA 재시작/중단을 단계별로 검증. 기존 intent/key/receipt를 조회/재사용하고 성공 작업 중복 생성 방지 |
| Q10a/b | 품질 기준선과 한 원인 개선 | 고정 자료/질문으로 검색·답변·인용·모름·지연을 분리 평가. 가장 큰 실패 한 곳만 개선 |
| Q11 | 사용자 최종 인수 | 새 업로드→CPU 준비→색인→Tool/Agent→head→질문→답변/출처→추가/제외를 한 화면에서 수행. 수동 개발 명령을 계속 요구하는 자동화 공백을 해결 |

Q06/Q07/Q08 adapter/화면 구현·로컬 인수와 첫 운영 Tool/Agent/head는 확인한 범위에서 완료했다. 실제 질문 가능 상태는 현재 question-policy를 따른다. 최초 smoke 답변/S1 성공은 PC 코드 질문과 일반 의미 품질의 성공이 아니다. 실패한 draft 후보는 활성화하지 않고 원래 published Agent/head1을 유지한다. 기존 초기화/migration·성공한 정제/Flow/model 생성을 반복하지 않는다.

위 단계의 구현→관련 검사→필요한 서버 인수→결과 기록을 완료하고 다음 의존성 만족 단위로 진행한다. 현재 환경/ID는 [인수인계](../HANDOFF_NEXT_SESSION.md), 과거 로컬 검사 수치는 [상태표](../status.md)의 최초 인수 이력을 따른다.

## Q06에서 먼저 해결할 저장·버전 계약

현재 [uploads.py](../../src/apps/document_qa/uploads.py)는 `PRAGMA user_version=1`과 `CHECK(question_revision=0)`을 유지하고 질문 가능 상태를 false로 투영한다. Q06b-1의 [question_store.py](../../src/apps/document_qa/question_store.py)는 별도 `question_head.revision`을 새 질문 코드의 기준으로 저장한다. 기존 library 컬럼은 구버전의 호환 값이다. [contracts.switch_head](../../src/apps/document_qa/contracts.py)는 순수 CAS 계약이며 [question_head.py](../../src/apps/document_qa/question_head.py)가 Q06b-3의 전체 재검증과 자기 DB 원자 전환/receipt를 구현한다. 따라서 flag만 true로 바꾸는 패치로 Q06을 완료할 수 없다.

Q06b-1은 기존 table/데이터를 재작성하지 않는 atomic additive migration과 불변 대상 참조를 구현했다. migration/후보 저장 직렬화·프로세스 중단 복구·재실행·기존 업로드/HTTP API·구버전 호환/복귀를 개발 환경에서 검사했고 서버 확장 schema·빈 head/revision을 확인했다. 기존 table·intent/key·receipt·원본 작업 이력·키를 삭제해 초기화하지 않는다. **Q05의 indexing_generation과 question_head.revision은 별도 값**이다. Q06b-2 Agent/질문 adapter와 Q06b-3 CAS/receipt는 구현했으며 첫 실제 전환/head1과 PC 질문 접수까지 확인했으며 답변/품질·두 문서 이후 전환은 후속 인수다.

질문 대상에는 index/version·문서 snapshot·embedding 배포/공간·Tool/Agent 버전과 검증 근거를 함께 고정한다. Knowledge active version 변경만으로 이전 Tool의 `index_version_id`가 자동 변경되지 않는다. Tool 결과의 `result.data/metadata` wrapper와 Connection 버전 pin을 검증한다. 새 head 전환은 자기 저장의 한 트랜잭션에서 expected revision을 확인하고, 실패하면 기존 정상 head를 유지한다. 과거 질문은 시작 당시 head를 보존한다.

## 처리와 인수 규칙

1. 접수 receipt와 도메인 완료를 구분한다. 불확실한 접수는 같은 intent/key/리소스 조회로 확인하며 자동 새 원본·새 key·중복 POST를 만들지 않는다.
2. n8n 첫 execution/grant·단계/시간 예산·고정 callback을 유지한다. 이미 저장된 paused_check의 `may_enable_inference=false`를 바꾸지 않고, 실제 GPU 실행은 명시적인 새 권한/세대 계약으로 구현·검증한다.
3. 불완전/OCR 미준비/빈 canonical·검색/Agent 실패를 질문 준비 완료로 표시하지 않는다. 문서 일부만 조용히 잘라 한도를 우회하지 않는다.
4. 화면은 문서/답변 HTML을 실행하지 않고 실제 근거 위치를 표시한다. 질문에 대한 자료가 없으면 근거 부족을 답하며 내용을 지어내지 않는다.
5. 지연 목표는 첫 실제 연결에서 시간을 측정한 뒤 정한다. 모델 준비 미완료와 답변 품질 실패를 구분한다.
6. 한 작업은 구현→관련 검사→필요한 서버 연결 인수→결과/다음 작업 기록으로 끝낸다. 같은 긴 진행 로그를 README/계획/HANDOFF에 복사하지 않는다.

최종 필수 사례는 첫 문서, 두 번째 문서 보존, 문서 제외/마지막 제외, 중복·응답 유실·새로고침·재시작, 정제/색인/Agent 실패, 정보 없는 질문, 의미상 맞는 답변·출처다. 실제 입력 크기·문서/청크 수·업로드/정제/색인/검색/답변 시간을 기록한다. 새 대형 원문/이미지/중복 snapshot은 프로젝트 밖에 두고 [보관 정책](../testing/artifact-retention.md)에 따라 핵심 결과만 남긴다.

## 후속 경계

현재 SQLite/file lock은 단일 Linux 호스트·전용 볼륨 adapter다. Kubernetes는 API/자산 소유권을 유지하되 단일 replica/PV 또는 분산 저장/실행권, 네트워크·credential 공급을 별도 인수한다. 실제 이식·재부팅·Ops 운영 백업/복원과 추가 자동화는 기존 보류다. Docker 백업 완료를 Kubernetes 계획 논의의 선행 조건으로 두지 않는다.

10월 12일 채택한 Dataset 이전·Airflow·HWP·provider/LiteLLM·MCP·세션/기억·문서 Tool·그래프 평가·CI/CD는 [확장 계획](platform-expansion-work-plan-20261012.md)의 별도 완료선으로 진행한다. BM25/RRF·ZenML·다중 GPU/노드·범용 자율 그래프·API 카탈로그 자동완성 등은 별도 검토다. 문서 QA 첫 인수의 선행 조건으로 추가하지 않는다. 현재 API와 명령은 [앱 안내](../../src/apps/document_qa/README.md), [연결 계약](../document-qa/contracts.md), 환경 재개는 [인수인계](../HANDOFF_NEXT_SESSION.md)를 따른다.
