# 앞으로의 작업 순서

2026-10-17 PR25 후속 자동 대응: 제출 전 고정한 UUIDv4와 exact userMessage 원문으로 turn/item을 자동 연결한다. 완결된 pre/post 이력·5초 fence·신규 claim·dispatch 기록을 요구하며 중복/본문 불일치/불완전 조회는 미확정, 재제출은 금지한다. [새7그룹 최초 PASS·단일 의미 변이 검출·최종 독립 PASS](testing/codex-owner-message-correlation-observed-20261017.json)를 보존했다. 실제 고정 test runner 결과와 standalone reviewer 응답을 원본 bytes/hash/이전 단계 digest로 연결했다. Draft 실제 응답은 게시 후 전용 체크포인트에 기록한다. native ACK/clientId 동일성·인증된 required-check 발행·고정 role workflow는 미인수이며 owner RPC/실제 queue/설치/서비스/PAUSE 변경0, 기존 PASS·패키지·원본11개를 유지한다.

2026-10-17 준비 후속: CLI/controller 로컬 연결과 지원되는 readonly turn 이력 계약을 확인했다. [상태](status.md)와 [설치·연결 절차](operations/codex-owner-cli-controller.md)를 기준으로 다음 필수 작업은 기존 소유 client가 확인한 실제 turn binding 또는 공식 CLI 접수→turn 대응 계약 확보, 인증된 tests/reviewer/Draft PR 근거 연결과 별도 native 인수다. 새 source의 실제 설치·PAUSE 전환 승인을 합치지 않는다. F0a 기능 시작 전 준비 게이트는 유지한다.

2026-10-17 owner ACK adapter: PR21 고정 commit 위에 기존 소유 client의 decoded 결과를 제한된 canonical bytes/hash 또는 고정 오류 코드로 준비하는 순수 함수를 추가했다. [이번 단위 근거](testing/codex-owner-ack-adapter-observed-20261017.json)는 이전 ACK 원장 PASS와 분리한다. 새4개 최초 실행 PASS(FAIL0/ERROR0/SKIP0)·의미 변이2개 검출(각 FAIL1/ERROR0, 실제 잘못 준비된 bytes/hash)과 임시 SQLite 저장·재개방 결합을 확인했다. 실행과 체크포인트 갱신을 묶었으며 최종 독립 판정은 같은 근거의 고정 보고서를 따른다. 실제 client 호출·controller 연결·두 native 작업은 미검증이고 PAUSE/D18 보류를 유지한다.

2026-10-17 ACK: serialized decoded 결과의 후보/UNKNOWN 불변 원장을 새4개 최초 PASS·2의미 변이 검출로 검증했다. 최종 독립 검토·Draft PR/readback으로 보존한다. 다음 구현은 기존 소유 client의 bounded serialization·오류 adapter 및 claim→submit/turn/result 연결이다. 실제 client 신뢰 pin/설치·상태 경로와 native 인수는 별도 조건이며 PAUSE/D18 보류를 유지한다.

2026-10-17 U01b2: owner quota-only reader와 durable prebinding의 새6개(3+2+1 재사용), 지정2 의미 변이 검출을 완료했다. 최종 독립 검토 후 별도 Draft PR로 보존한다. 다음 작은 구현은 기존 소유 client 요청의 ACK 보존·turn/result 결합이다. 실제 설치·신뢰 pin/상태 경로·role/owner 입력과 native 인수는 별도 조건이며 PAUSE/native D18 보류를 유지한다.

2026-10-17 KST U01b2: PR18 고정 `780d0755378b9f95f0282ec766ca99300b2b0b53` 위에 오프라인 queue/list 관측기를 추가했다. 기존 공개 `read_intent` 한 번으로 원래 intent를 읽고, client·prompt bytes·thread·원문 hash와 완결 pagination의 유일 후보를 대조한다. 전용 임시 SQLite/독립 소스 복사본의 새4개 최초 PASS와 두 의미 변이 검출(각 FAIL1/ERROR0), 원래 UNKNOWN/raw/hash/revision 및 재개방 보존을 확인했다. 기존 PASS는 반복하지 않았다. 최종 독립 검토는 별도 근거에 기록한다. 실제 ACK 저장·turn/result·controller/scheduler 연결과 두 실제 작업 연속 실행은 미완료이며 runtime D18 보류·두 PAUSE·운영/인증정보/main/VMS 보호를 유지한다. [새 근거](testing/codex-queue-correlation-observed-20261016.json).

2026-10-16: [제출 intent 저장 단위](testing/codex-unattended-intent-observed-20261016.json) 이후에는 U01a·저널·native queue의 ACK/불명 재조회와 완료/검토/Draft PR readback을 연결해야 한다. 실제 owner 설정·native 계약/사용량/role 권한을 확인하고 별도 최소 계획을 독립 검토한 뒤 연결한다. D18 runtime 보류·두 PAUSE·agent_project 한정은 유지하며 VMS·worker·scheduler를 활성화하지 않는다.

2026-10-16: [U01a 순수 후보 선택](testing/codex-unattended-admission-observed-20261016.json)을 실제 dispatch와 분리한다. 다음 U01b는 보호된 기존 controller의 queue intent/ACK·내구성·결과/독립 검토·Draft PR readback 연결이다. native 계약·소유자 설정·한도·권한·PAUSE 전제가 확인되기 전 worker나 F0a를 활성화하지 않는다.

2026-10-15 fixture 연결: [설정 로더](codex/github-fixture-config.md)의 개발 인수 범위와 남은 조건은 [상태](status.md)·[근거](testing/github-fixture-config-observed-20261015.json)를 따른다. 다음 연결에는 소유자가 승인한 실제 version/hash·UID·설치/상태/DB 경로의 보호된 입력과 설치 인수가 필요하다. 그 뒤 tests/reviewer supervisor·issuer/서명·최신 binding/검사 발행을 별도 범위로 연결한다. 기존 sandbox 조사·운영·worker/scheduler 보류를 유지한다.

2026-10-13 규칙 준비: 공통 정책과 템플릿·프로젝트별 책임·첫 작업 연결을 [Codex 문서](codex/README.md)에 정리했다. 문서의 GitHub main 반영과 서버 실행 환경 적용은 아직 완료되지 않았다. [현재 상태](status.md)를 기준으로 읽고 본문에 보존한 과거 근거와 구분한다.

2026-10-13 추가: src 구조·Ops 통합의 로컬 적용과 검증은 완료됐다. [소스 구조](architecture/source-layout.md)와 [native 검사 근거](testing/source-layout-native-20261013.json)를 따른다. 구조의 GitHub main 반영은 `6dd46d069079fc20df5b3d4bac71fe3450550bf8`에서 확인했다. 현재는 [Codex 문서·실행 준비](codex/README.md) 단계다. 이후에는 본격적인 Codex 기능 개발 전에 사용자에게 먼저 알리고 정리한 공통 규칙·구성·프로젝트 문서를 기준으로 실제 서버 실행 준비를 확인한다. 이 준비 전에는 기능 개발이나 무인 실행을 시작하지 않는다. 아래 날짜별 인수 상태와 운영 자산은 유지한다.

공통 규칙·구성·템플릿 준비 후 첫 Agent 기능 작업은 **문서 QA의 인용 누락 해결**이다. 첫 TXT의 실제 GPU 색인·검색 Tool·QA Agent smoke/발행·head revision1은 확인됐지만 PC 코드 질문과 prompt-only 초안의 답변 인용은 실패했다. 최신 근거와 한계는 [상태표](status.md), 실제 환경과 재개 위치는 [인수인계](HANDOFF_NEXT_SESSION.md)에 둔다.

| 순서 | 작업 | 완료선 |
|---|---|---|
| 0 | F0a 첫 문서 답변/출처 | 코드·담당 부서·근거 없는 질문 확인, 인용 gate·기존 head/실패 이력 보존 |
| 1 | F10a CI 기준선 | 실제 runner에서 CPU/계약/화면 필수 검사, 지원 Node의 실제 JS 실행 |
| 2 | F0b 문서 QA 제품 인수 | 두 번째 문서 누적·추가/제외·중복/유실/재개·품질·업로드부터 한 화면 경로 |
| 3 | F1 학습데이터 Data 이전 → F5 형식/RAFT | legacy 입력/키 보존, 불변 Data 버전·변환/생성·Training 호환 |
| 4 | F2 Data 일정/Airflow → F6 HWPX/HWP | 하나의 일정 소유자·구간 key·backfill, parser별 canonical/원본 회귀 |
| 5 | F7 상용 API/F3a LiteLLM → F4 기본 MCP | Model backend/capability·비용/credential 경계, 날짜/시간대 Tool |
| 6 | F8 세션/기억 → F9 문서 Tool | turn·권한/자료 pin, 명시 기억, 서식 채우기·수정·원본 보존/렌더 |
| 7 | F3b/c LightRAG/Neo4j 평가 → F10b CD | 고정 검색 비교/출처/비용, 선택 저장소, staging·image pin·readback·복귀 |

10개 사용자 요구의 책임·의존성·첫 작은 단위·단계별 검증과 Codex 연속 작업 규칙은 [플랫폼 확장 계획](plans/platform-expansion-work-plan-20261012.md)에서 관리한다. 이 순서는 범위를 채택한 설계 결정이며 모든 infra를 바로 설치하거나 운영 서비스를 자동 재시작하는 지시가 아니다. 기존 n8n/Langfuse를 재사용하고 LightRAG/Neo4j는 평가 결과에 따라 채택/종료한다. CI는 초기부터 확대하고 운영 CD는 후속 인수한다.

문서 QA의 상세 계약/Q05c-2·Q09~Q11은 [기존 계획](plans/document-qa-work-plan-20261002.md)을 유지한다. 실제 question-policy의 질문 가능과 답변/의미 품질 인수는 구분한다. static production_* flag나 과거 보류 실행권을 바꿔 완료로 대체하지 않는다.

Ops 운영 백업/복원·추가 운영 자동화·실제 재부팅·Kubernetes는 보류이며 backup_ready=false다. DDP/원격 다중 노드·ZenML·범용 자율 그래프·외부 알림·전체 복구는 별도 검토 범위다. [백업 범위](operations/backup-restore.md), [설계 원칙](design-principles.md)을 따른다.


## 2026-10-17 소유 client의 제한된 metadata 연결

다음 연결 조건: 최종 검토 source/transport manifest와 전용 설치·state·DB 경로/UID의 소유자 승인 → 기존 owner thread/client binding → queue 조회의 읽기 전용 handler 계약 → 실제 controller launcher와 승인된 작은 작업 두 건의 순차 인수. 설치된 legacy never/ephemeral runner 및 VMS 포함 console은 자동 전환하지 않는다. 현장7RPC 관측과 수정 제품 native 인수는 구분한다.
