# 프로젝트 현재 상태

2026-10-13 규칙 준비: 공통 정책과 템플릿·프로젝트별 책임·첫 작업 연결을 [Codex 문서](codex/README.md)에 정리했다. 문서의 GitHub main 반영과 서버 실행 환경 적용은 아직 완료되지 않았다. 현재 준비 상태는 아래 표에서 관리한다.

| 준비 항목 | 현재 판정 |
|---|---|
| 공통 정책·프로젝트 규칙·템플릿 | 1.2-preparation 배포판 준비 |
| 이번 문서의 GitHub 전달 | 2026-10-14 privateInt 연결·두 저장소 쓰기 권한 확인. [준비 PR #1](https://github.com/privateInt/agent_project/pull/1)에서 게시·검토 결과 확인. main 반영은 독립 검토 후 |
| GitHub 필수 검사·독립 승인 강제 | 현재 main 보호 없음·ruleset 없음·필수 검사 목록 비어 있음. 자동 개발 전 적용 필요 |
| GitHub 자동 병합 | 현재 꺼짐. PR 게시와 별개이며 아직 활성화하지 않음 |
| 서버 CLI·ChatGPT 계정·모델·동시 실행 | 실제 확인 전 |
| 크레딧 미사용 제어 | 확인 전·무인 실행 보류 |
| 독립 reviewer 경로 | 별도 reviewer의 문서 검토·도구 사전 계획 검토 수행. GitHub 필수 검사 발행/강제는 미구현 |
| 모델 호출 없는 조회 도구 | [사용 범위](codex/common/READINESS.md)와 [합성 검사 근거](testing/codex-readiness-20261014.json). 실제 서버 조회·크레딧 강제는 미확인 |
| 기능 개발·worker·scheduler | 미시작 |

2026-10-13 추가: src 구조·Ops 통합의 로컬 적용과 검증은 완료됐다. [소스 구조](architecture/source-layout.md)와 [native 검사 근거](testing/source-layout-native-20261013.json)를 따른다. QA 581개는 검증된 기존 근거를 재사용했고, 수정 관련 검사와 남은 도구·infra·화면 검사를 마쳤다. 새 회귀는 없으며 기존 Ops adapter 실패 1개와 선택 native 검사 skip은 유지한다. 구조의 GitHub main 반영은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`에서 확인했다. 현재는 [Codex 문서·실행 준비](codex/README.md) 단계다. 본격적인 Codex 기능 개발 전에 사용자에게 먼저 알리고 정리한 공통 규칙·구성·프로젝트 문서를 기준으로 실제 서버 실행 준비를 확인한다. 기능 개발·무인 실행·scheduler 활성화는 시작하지 않았다. 아래 날짜별 인수 상태와 운영 자산은 유지한다.

갱신: 2026-10-12. 첫 TXT의 실제 GPU 색인/활성화·검색 Tool 시험/발행·고정 QA Agent smoke/발행·첫 질문 head CAS revision1·인증된 question-policy의 question_ready=true를 사용자 출력으로 확인했다. PC 화면의 서버 UUID v5 호환 수정 후 질문은 접수됐으나 ANSWER_REFERENCE_INVALID/MISSING_REFERENCE로 실패했다. 인용 prompt-only 초안도 동일 실패이며 발행/활성화되지 않았다. 현재 답변 모델은 google/gemma-3-1b-it이다. 첫 PC 답변/출처·폭넓은 의미 품질 인수는 미완료다. [최신 관찰](testing/document-qa-live-observed-20261012.json)은 당시 사용자 출력/화면 근거이며 현재 호스트를 직접 재조회한 결과는 아니다.

10개 확장 요구를 [플랫폼 확장 계획](plans/platform-expansion-work-plan-20261012.md)으로 채택했다. 학습데이터 Data 이전·Airflow·provider·HWP·세션/기억·문서 Tool·CI/CD는 계획이며 미구현/미인수다. n8n/Langfuse는 기존 구현/과거 인수 재사용, LightRAG/Neo4j는 별도 평가 단계다. 공통 규칙·구성·템플릿 준비 후 첫 Agent 기능 작업은 F0a이며 개발 규칙은 [AGENTS.md](../AGENTS.md)를 따른다. Ops 보류는 유지한다.

## 문서 QA 완료와 남은 범위

아래 Q01~Q08 표는 최초 구현/해당 날짜의 서버 로컬 인수 이력이다. head0·GPU 보류·미실행 수치는 그때의 값이며 현재 상태가 아니다. 10월 12일 후속 실제 인수와 현재 실패는 표 다음의 최신 관찰을 따른다. 과거 날짜별 JSON은 덮어쓰지 않는다.

첫 제품 목표는 사용자가 문서 업로드와 질문만으로 답변/출처를 얻는 서비스다. Q01~Q04b, 기존 TXT 1개의 실제 CPU 연결과 Q05a 후보 plan·Q05b 실제 보류 callback까지 확인했다. Q05a/Q05b의 전체 QA 276/324개는 당시 개발 환경 검사이며 사용자 서버의 해당 단계 검사는 35/48개다. Q06a 전체 QA 376개는 개발 환경 검사이며, 관련 52개는 사용자 서버에서도 통과했다. 관련 검사는 임시 상태·대역/loopback HTTP를 사용하며 운영 Tool 인수를 대신하지 않는다.

| 단계 | 구현/검증 | 실제 서버와 남은 범위 |
|---|---|---|
| Q01 | 순수 계약/fixture 28개 검사 | applied/written=15, 28개 OK [근거](testing/document-qa-q01-server-observed-20261002.json) |
| Q02 | 조회/선택/자기 기본값 저장, 29개 검사 | applied/written=17, 29개 OK, 네 플랫폼 API reachable. embedding/chat 호환 실행 후보 없음, defaults_ready=false. 당시 네 배포는 모두 stopped. [근거](testing/document-qa-q02-server-observed-20261002.json) |
| Q03a | [업로드 접수 API/자기 저장](document-qa/uploads.md), 중복·동시 제한· 응답 유실·중단·공간 회수 검사 41개 OK | applied/written=18, 서버 41개(5.882초) OK, init/status passed. 최초 초기화 당시 job 없음. 동일 자료함에서 기존 TXT job의 명시 재시도와 Data 200 receipt를 후속 확인했다. 신규 QA 업로드 전체 경로는 별도 인수 대상이다. [서버 근거](testing/document-qa-q03a-server-observed-20261007.json) |
| Q03b | [n8n 전달/receipt](document-qa/workflows.md), 새 검사 36개 OK | applied/written=19, 서버 36개(1.803초) OK, workflow init/status passed. 최초 init은 configured=false였다. QA 인증·진입 Flow prepare/active/정의·버전 일치와 실제 TXT job의 n8n 진입 callback receipt를 확인했다. [서버 근거](testing/document-qa-q03b-server-observed-20261007.json) |
| Q04a | [CPU 정제 adapter](document-qa/refinement.md), 새 검사 41개 OK, 기존 TXT/Markdown 파서 출력 호환 확인 | 서버 applied/written=18·41개(1.413초) OK·init/status passed, 최초 초기화 당시 동일 자료함/job 없음. [서버 근거](testing/document-qa-q04a-server-observed-20261007.json). 운영 TXT 정제·canonical 본문은 후속 확인했다. Markdown/PDF/OCR 인수는 미실행 |
| Q04b | [별도 n8n CPU Flow/실행권](document-qa/refinement-workflow.md), 새 32개 OK·기존 Q03b 4개 호환. 최초 execution/grant·40 step/600초·명시 resume | 서버 applied/written=20·32개(3.020초) OK·init/status passed. 최초 init은 configured=false였다. Wait 비교 수정 적용·9개 검사 후 기존 CPU Flow prepare/활성 정의·게시 버전 확인과 QA 수동 재실행 완료. [서버 근거](testing/document-qa-q04b-server-observed-20261007.json). 실제 TXT job의 두 callback·Data 정제·canonical_expected_text_verified=true, 2 step 완료. canonical_verified도 question_ready=false |
| Q05a | [색인 adapter](document-qa/indexing.md), 새 35개·전체 QA 276개 검사 OK | 개발 환경의 임시 SQLite·Knowledge 대역·loopback HTTP만 검사. 서버 applied/written=13·35개(1.564초) OK·init/status passed·기존 TXT canonical 후보1개/1024bytes plan passed. [서버 근거](testing/document-qa-q05a-server-observed-20261007.json). n8n/실제 embedding·build·검색은 미실행. [오프라인 근거](testing/document-qa-q05a-offline-20261007.json) |
| Q05b | [색인 workflow](document-qa/indexing-workflow.md), 새48개·전체QA324개 OK | 서버 applied/written=17·48개(7.989초) OK·init/status passed. 별도 Flow 등록/게시 정의 확인과 최초 execution=8·generation=1·steps=1의 accepted/blocked callback 통과. paused_check/GPU_VERIFICATION_DEFERRED, 저장된 추론 금지 실행권 확인. Data/Knowledge POST=0·추론/GPU 요청 없음. 실제 색인은 미실행. [서버 근거](testing/document-qa-q05b-server-observed-20261007.json) · [오프라인 근거](testing/document-qa-q05b-offline-20261007.json) |
| Q06a | [검색 Tool adapter](document-qa/search-tool.md)·추가 테이블·Connection/색인/모델 고정·intent/receipt·시험/발행 조회 gate, 새52개·전체QA376개 OK | 서버 applied/written=0: 대상 13파일 내용이 패키지와 이미 일치. 관련52개(4.399초) OK·init/status passed, upload_schema=1·plan_count=0·question_ready=false·production_tool_verified=false·production_api_requests=0. 추론/GPU 요청 없음. 운영 Tool 시험/발행·실제 QA 색인/질문은 보류. [서버 근거](testing/document-qa-q06a-server-observed-20261008.json) · [오프라인 근거](testing/document-qa-q06a-offline-20261008.json) |
| Q06b-1 | [질문 대상 저장/migration](document-qa/question-storage.md)·별도 head/revision·불변 후보·atomic migration, 새40개·전체QA416개 OK | 서버 applied/written=13·관련40개(2.199초) OK·migrate/status passed. question_storage_schema=1·upload_schema=1·head_revision=0·head_target_id=null·target_count=0·legacy_question_revision=0. production_api_requests=0·추론/GPU 요청 없음. 전체QA416개는 개발 환경 검사다. Q06b-1은 질문 저장만 추가한 단계이며 당시 head 활성화·AgentRun·질문 실행은 미구현이었다. question_ready=false. [서버 근거](testing/document-qa-q06b1-server-observed-20261008.json) · [오프라인 근거](testing/document-qa-q06b1-offline-20261008.json) |
| Q06b-2 | [고정 QA Agent/질문 adapter](document-qa/qa-agent.md)·chat/Tool/Connection pin·intent/receipt·시험/발행·접수 당시 head 고정·원문 인용 검증, 개발 새45개·전체QA461개 OK. 임시 SQLite·독립 Agent 대역·인증 loopback HTTP에서 중단/동시 접수·기존 저장 보존 검사 | 서버 applied/written=16·관련 45개(9.258초) OK·init/status passed. qa_agent_schema=1, plan/question/target=0, head revision=0·대상 없음, upload schema=1·호환 question_revision=0. 운영 API·추론/GPU 요청 없음. Q06b-2 당시 기본 serve는 새 질문 adapter를 자동 연결하지 않음. CAS·운영 AgentRun/GPU·질문 품질 인수는 후속, question_ready=false. [서버 근거](testing/document-qa-q06b2-server-observed-20261008.json) · [개발 근거](testing/document-qa-q06b2-offline-20261008.json) |
| Q06b-3 | [검증된 질문 head CAS](document-qa/question-head.md)·읽기 전용 전체 색인/Tool/Agent/시험 재검증·대상/head/receipt 원자 commit·같은 요청 키 복구·기본 serve 연결, 개발 새47개·전체QA508개 OK | 서버 applied/written=16·관련47개(14.385초) OK·init/status passed. question_head_schema=1·head_activation_implemented=true, head revision=0·대상 없음·target/activation/plan/question=0, head_active=false·question_ready=false. 운영 API·추론/GPU 요청 없음. 임시 SQLite·독립 대역·인증 loopback HTTP·프로세스 중단·동시 전환·두 문서와 과거 질문 보존은 검사 대역의 근거다. 전체QA508개는 개발 환경 검사이며 기본 serve의 실제 기동·운영 대상 전환·질문/의미 품질은 후속 인수. [서버 근거](testing/document-qa-q06b3-server-observed-20261008.json) · [개발 근거](testing/document-qa-q06b3-offline-20261008.json) |
| Q07a | [업로드·자료 상태 화면](document-qa/screen.md)·기존 인증/업로드/작업 GET 연결·같은 요청의 파일 해시/키 복구·5초 조회, 개발 HTTP12개·화면 대역25개 OK | 서버 applied/written=17·관련12개(0.583초) OK·status passed, screen_implemented=true·q07a.1·/console. 기존 문서1개·canonical_verified·색인 not_requested/Flow blocked(GPU_VERIFICATION_DEFERRED), head revision=0·전환0·question_ready=false, 운영 API 요청0·추론 요청false. DB/migration·추론 옵션 변경 없음. 실제 QA API 재실행·브라우저/모바일·새 업로드 전체 경로는 미확인. 전체QA520개·화면 대역25개는 개발 환경 검사다. [서버 근거](testing/document-qa-q07a-server-observed-20261008.json) · [개발 근거](testing/document-qa-q07a-offline-20261008.json) |
| Q07b | [오류·CPU 재개 화면](document-qa/error-recovery.md)·현재 상태별 동작·실행 전 revision 재검증·명시 확인·탭 기록/GET 복구, 개발 새 HTTP20개·화면 대역24개 OK | 서버 applied/written=21·관련32개(1.980초) OK·status passed, q07b.1·screen_recovery_implemented=true·upload_and_CPU_only. 기존 문서1개·canonical_verified·색인 not_requested/Flow blocked(GPU_VERIFICATION_DEFERRED), head revision=0·전환0·question_ready=false, 운영 API 요청0·추론 요청false. 전체QA540개·JS 화면 대역49개는 개발 환경 검사. 같은 상태 동시 요청의 한 번 전달·원래 upload/refine key/body·CPU 세대/receipt·완료/terminal 보호·저장된 GPU 금지 실행권 유지 확인. 새 DB/init·추론 권한 변경 없음. 실제 QA API 재실행·브라우저/운영 재개는 미확인. CLI job 조회는 recovery 동작 목록을 제공하지 않는다. [서버 근거](testing/document-qa-q07b-server-observed-20261008.json) · [개발 근거](testing/document-qa-q07b-offline-20261008.json) |
| Q08 | [질문·답변·출처 화면](document-qa/question-screen.md)·현재 policy GET·질문 접수/첫 실행·기존 질문의 명시 실행/동일 요청 재확인/결과 조회·과거 자료 버전·검증 원문/블록/실제 페이지 표시, 새 HTTP10개·질문 화면 대역36개 OK | 관련 화면42개·전체QA550개·세 화면 DOM/fetch 대역85개는 개발 환경 검사. 자동 갱신은 로컬 GET만 수행. 불확실 질문/실행은 탭 기록과 원래 key/intent로 명시 복구. 실패·근거 부족·답변 잘림을 구분하고 외부/임의 원문 URL을 만들지 않음. 새 DB/init·질문/색인 추론 권한·head 변경 없음. 서버 applied/written=20·관련42개(5.166초) OK·status passed, q08.1·질문 기능 구현 표시·정책 경로 확인. 기존 CPU 완료/GPU 대기·head0/대상 없음/전환0·질문0·question_ready=false, 운영 API 요청0·추론 요청false. 정책 경로 표시는 실제 HTTP 조회 근거가 아님. 실제 API 재실행/브라우저/운영 질문/품질은 미확인. [서버 근거](testing/document-qa-q08-server-observed-20261008.json), [개발 근거](testing/document-qa-q08-offline-20261008.json) |

[TXT 실제 CPU 인수](testing/document-qa-cpu-live-observed-20261007.json)는 실패 2회 뒤 같은 job/key의 명시 세 번째 Data 전송 200·진입/CPU n8n callback·Data 정제·예상 본문을 2 step 뒤 확인한 범위다. 신규 업로드 전체 경로·Markdown/PDF·timeout/재시작 인수를 대신하지 않는다.

### 2026-10-12 실제 연결과 현재 막힘

| 범위 | 마지막 확인 | 남은 것 |
|---|---|---|
| 기존 TXT 정제·색인 | canonical1개, native index/version active, chunk/indexed1개. gen2 설정 오류 후 수정/새 세대 인수 | 신규 업로드 전체 자동 경로·두 번째 문서·추가/제외/복구 |
| 검색 Tool | 고정 index/model/Connection, 실제 반환1개·원문 참조 확인, published version | 다양한 질문 검색 품질 |
| QA Agent smoke | 실제 answered·S1 원문, published AgentVersion | 특정 PC 질문 성공·폭넓은 의미 품질 |
| 질문 head/정책 | CAS0→1 applied, target01851866…·인증 policy question_ready=true | 실패 후보는 전환하지 않음. 정책 ready는 모든 질문의 성공 보증이 아님 |
| PC 화면/UUID | LAN 렌더·인증·기존 자료 조회·질문 가능/접수 확인. UUID v5 fix applied | 실제 코드/부서 답변·출처 및 좁은 화면·새 업로드/재개 인수 |
| PC 코드 질문 | Run f8923983… failed, generate 성공/finalize 인용 누락, stop·13토큰 | 해결 전이며 이 실패를 유지 |
| prompt-only 초안 | draft73053c8f…/Run ba35175e… failed, 인용 누락·stop·21토큰/49bytes | 발행/활성화 안 됨. 다른 비교가 필요하면 별도 계획·실행 |
| Node 검사 gate | 사용자 Node12.22.9, 화면58개 OK(skipped=1). 개발 지원 Node에서63개 OK | 사용자 신규 JS integration 클래스는 skip, 통과로 계산하지 않음. CI는 지원 Node 사용 |

전체 자산 ID·receipt·hash·단계 요청 수·진단과 품질 한계는 [새 관찰 근거](testing/document-qa-live-observed-20261012.json)에 둔다. 인용 누락은 출력 길이 제한으로 확인되지 않았다. 구형/정적 모듈의 question_ready/production flag는 최신 HTTP 정책·실제 실행 결과와 구분하며 값을 수정해 인수로 바꾸지 않는다. Q05b gen1 GPU 보류와 gen2 INDEXING_PROFILE_INVALID, 두 실패 Run/질문/plan·원래 intent/key/receipt는 보존한다.

다음은 F0a 인용 준수 원인을 좁게 검증하고 첫 문서 답변/출처를 인수하는 작업이다. 재개 시 기존 초기화·성공한 업로드/정제·모델 생성·Flow 등록/전환을 반복하지 않는다. 그 뒤 CI 기준선과 두 문서/Q09~Q11, 확장을 [로드맵](roadmap.md)대로 진행한다. GPU는 사용자 10월 12일 갱신으로 문서 QA에 사용 가능해졌고 Ops 백업·재부팅·Kubernetes 보류는 유지한다.

## 상태를 읽는 기준

- **구현 확인**: API 뒤의 처리·저장·실행 코드를 확인했다.
- **과거 서버 확인**: 저장된 서버 결과 또는 당시 사용자 확인 기록이다. 현재 서버 재검증을 뜻하지 않는다.
- **미완료·보류**: 구현 부재, 실제 인수 미확인, 사용자 보류를 구분한다.

2026-10-01 종합 코드 검토의 새 검사는 Python 314개 통과·45개 건너뜀, Node 화면 계약 8개 스크립트 통과다. 당시 Go 빌드·DB/GPU·실제 브라우저·서버는 재실행하지 않았다. 이후 Ops 진단 보완의 선택 검사 341개 중 286개 통과·55개 건너뜀과 사용자 제공 서버 복구 결과는 별도 근거다. [분석과 근거](reviews/code-review-20261001.md), [종합 검증](testing/source-review-20261001.json), [Ops 검사](testing/ops-changed-worker-inventory-offline-20261002.json)를 참고한다.

## 플랫폼별 완료 범위

| 영역 | 코드에서 확인한 구현 | 보존된 검증 근거 | 남은 범위 |
|---|---|---|---|
| Model 관리 | 모델/버전 등록·HF import·이름 변경·목록·숨김/영구 삭제·배포 삭제 | 단일 서버 v1 및 관리 기능 검사, 삭제 실사용 확인 | 전체 삭제·GPU 동시 로딩/OOM·범용 모델 확대의 운영 검증 |
| Model 학습 | Axolotl QLoRA·JSONL 고정 버전·취소/로그·출력 복구·checkpoint 재개·평가/비교 | Gemma 3 1B 단일 GPU의 짧은 학습·제어·출력/서빙 기록 | 로컬 DDP·원격 SSH 경로는 구현과 실제 인수를 구분. FSDP/DeepSpeed·다중 노드 학습은 현재 계약 밖 |
| Model 추론 | vLLM snapshot/adapter·chat, 지정 embedding/reranker/OCR profile | embedding/rerank·OCR 및 제한된 GPU 서빙 확인 | 임의 모델 자동 지원·LiteLLM 통합 없음 |
| Data | HTTP/Chromium 수집·원본 업로드·그룹·parser/OCR·JSON/Markdown·다운로드·삭제 | 수집/정제·OCR 연결·그룹 실사용과 부분 내용 보존 확인 | 어려운 표/그림·OCR timeout·PDF 제목 추정·ZIP 등은 보류/확장 |
| Knowledge | 두 청커·Model 임베딩·OpenSearch HNSW·버전 활성화/삭제·벡터 검색·선택적 rerank·평가 CLI | v1 핵심 경로·12문항 비교·격리 실패/복구 Go/DB 검사 | 최초 recursive 후보 불일치 원인 미확정. 최종 Gateway 수정 후 실제 화면 확인 일부 미확인. BM25/RRF 없음 |
| Agent | OpenAPI/직접 등록·Connection/Tool 버전·시험/발행·MCP·자연어 검색·고정 LangGraph QA·n8n 링크 | Tool/MCP 기반, 실모델 실행, n8n 전달/동일 요청 재사용·7개 컨테이너 재시작 보존 | 문서 QA 화면·답변 품질은 후속 계획의 인수 대상이며 미완료. 일반 Flow 편집은 보류. 범용 그래프·API 카탈로그 자동완성 미완료 |
| Ops 조회 | 4개 업무 플랫폼 API·자원 7개 그룹·Langfuse 추적·경보·LAN 화면·오류 구분 | 서버 조회/화면·재시작 보존·격리 장애 검사·O04 경보 기록 | 외부 알림 발송·다중 사용자 권한·장기 부하/보관 정책의 인수 |
| Ops 자동갱신 | Compose 발견·설정 생성/갱신·systemd worker·boot 정책·유지보수/복구 도구 | 10월 2일 명시 복구 PASS, 새 invocation의 정상 평가 1회·설정 readback 확인 | 실제 재부팅·장기 운영 인수는 미확인. Kubernetes 전환 미완료 |
| Ops 백업 | age 준비·운영 metadata 계획·측정 adapter·검토된 수집/명시 복구 실행기·격리 UID 파일 복원 | 운영 plan 서버 PASS, 기존 격리 native 16/16 PASS, 새 실행기 오프라인 검사 | 운영 측정/수집/복원은 보류. public `measure` 차단 유지, 최초 운영 세대 없음 |

## 이번 Ops 범위의 마무리

사용자가 제공한 2026-10-02 서버 결과에서 진단 보완 7파일 적용과 02:24:24Z 새 복구 `plan` PASS를 확인했다. 소유 서비스 9개·보호 running 55개·안정된 current inventory·backup helper 부재를 검토했다. 이어 해당 review ID의 명시 `apply`가 PASS였고 마지막 관찰은 **11:37:18 KST(02:37:18Z)**다.

- worker는 새 systemd invocation에서 `active/running`, `desired_state=running`, `recovery=null`, `evaluation_status=healthy`다.
- 새 정상 평가 1회와 현재 설정의 인증 readback을 확인했다. 과거 평가를 재사용하지 않았다. 관찰 시간은 3.038초이며 실제 재부팅·장기 안정성을 검증한 결과는 아니다.
- worker 제어는 2회(reset-failed와 단일 start)였다. 업무 서비스 제어·설정/코드/정책 변경·image build/pull·추론·백업·복원은 요청하지 않았다.
- 원래 `OTHER_CONTAINER_CHANGED` 실패와 첫 apply의 `MAINTENANCE_RUNNING_SET_CHANGED` 기록은 보존한다. 최초 바뀐 컨테이너와 원인은 여전히 미확정이다.

근거: [최신 복구 결과와 범위 결정](testing/ops-worker-recovery-closure-observed-20261002.json), [첫 apply 제출 전 차단](testing/ops-changed-worker-inventory-observed-20261002.json), [이전 failed/paused 상태](testing/ops-changed-worker-observed-20261002.json). 이후 사용자가 `ops_scope_closure_20261002` 문서 패치를 서버에 적용해 `status=applied`, `written=10`을 확인했다. 활성 runtime 변경·패치 백업 생성·서비스 제어·재시작 요구는 모두 false였다. 이번 Ops 범위는 마무리했으며 문서 변경을 이유로 복구 apply나 기동 검사를 반복하지 않는다.

## 보류한 백업과 운영 작업

운영 목적지·age 키 준비, 운영 plan PASS, 수집/명시 복귀 실행기 구현·오프라인 검사, 기존 격리 생성/파일 복원 16/16 PASS는 완료 근거로 유지한다. 운영 plan 당시 대상은 서비스 9개·저장소 8개·이미지 12개·보호 대상 58개였다. 복구 plan의 보호 running 55개와 조회 시점/범위가 다르므로 숫자만으로 타 서비스 변화 여부를 판단하지 않는다.

운영 측정은 보호 대상/metadata 오류와 worker 복구 요구로 중단됐다. 현재 public `measure`는 유지보수 조사 연결 전까지 `BACKUP_MEASURE_MAINTENANCE_REQUIRED`로 차단한다. 사용자 결정에 따라 그 연결 구현·실제 크기 조사·새 실행기의 native 연결 인수·최초 운영 수집·복원과 추가 운영 자동화는 이번 범위에서 보류한다. 성공 출력의 `next_step=reviewed_maintenance_size_survey_required`는 도구의 기존 후속 항목이며 현재 실행 지시가 아니다.

`backup_ready=false`다. 약 1.34TB의 보수적 상한/공간 문제, 개인키 별도 보관·application drain·실제 세대 DB/image/application 복원은 해결되지 않았다. 관측 백업과 기존 Model 백업도 별개이며 Data·Knowledge·Agent·n8n을 포함한 전체 복구는 미완료다. 실제 재부팅과 Kubernetes 이식은 보류를 유지하며, 이 백업 작업의 완료를 Kubernetes 계획 논의의 선행 조건으로 두지 않는다.

범위와 과거 실패의 상세 근거는 [백업 범위](operations/backup-restore.md), [실행기 계약](operations/ops-backup-execution.md)에 모은다. 다음 문서 QA 작업의 순서와 완료 기준은 [로드맵](roadmap.md)과 [작업계획서](plans/document-qa-work-plan-20261002.md)를 따른다.

## 품질과 성공 상태를 구분할 항목

- Agent의 개선 prompt 버전은 인용 형식·n8n 전달·재사용에 성공했다. 하지만 질문을 5회 반복하고 실질적 설명이 없었던 기록이 있어 의미 품질 인수는 통과하지 않았다. 별도 구버전 추적 실행의 `MISSING_REFERENCE` 실패도 유지한다.
- Knowledge의 12문항·한정 자료 비교는 실행됐다. 평가가 전혀 없다는 과거 안내는 더 이상 현재 상태가 아니다. 최초 recursive ON 후보 불일치 실패 1건은 재조회 성공으로 해소 처리하지 않는다.
- Data의 최종 자동 영역 실험은 9개 완료·품질 통과 0/9·timeout 2건이다. 다른 합성 OCR 성공 기록과 별개다. 사용자가 일부 오류를 허용하고 다음 단계로 진행한 결정을 유지한다.

상세 근거는 [Agent 결과](../src/platform/agent/tests/results/a09-server-20260925.json), [추적 실패 결과](../src/platform/agent/tests/results/o02-tracing-server-20260927.json), [Knowledge 평가](../src/platform/knowledge/tests/results/evaluation-candidate-diagnostics-20260921.json), [Data 상태](data-platform/status.md)에 있다.

현재 우선순위는 [로드맵](roadmap.md), 과거 단계 이력은 [이력 목차](history/README.md)에서 관리한다.
