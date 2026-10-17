# QUEUE-ACK-20261017 D29 독립 사전 검토

판정: **PASS** — 다음 exact v2와 아래 D06 명확화를 구현·검사 기준으로 고정한다. 제품 정적 검토·실행·최종 인수 PASS는 아니다.

- review-plan.md SHA256: `027cd5bda32eac67e91246b410d2177afb8a108830bde951ffeab7a0351521f7` (16,485 bytes / 99 lines).
- canonical unit-budget.json SHA256: `b81401334b875233b0a8034df275b6ea428aa34928bfa5c355b7438cfb355d1e`.
- 실제 clean workspace HEAD/base PR20: `aef3af98d1bbc9037f1b75906e0217844bb5bd1d`; branch `codex/agent-project-queue-ack-20261017`.
- ThreadQueueAddResponse.json SHA256: `c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314`.

**D06 경미 명확화 1 — 시간:** 계획 7행의 workspace 생성 기준 `01:52:09.392724Z→02:52:09.392724Z` 대신, plan-freeze.json 및 canonical budget의 **2026-10-17 01:50:11.827731Z→02:50:11.827731Z**를 반드시 사용한다. 최초 CLI/schema 분석과 이번 검토·구현·검증·후속 정리 시간을 모두 포함한다. 실행기·중단 gate·새 결과에도 이 더 이른 마감을 적용한다. 이전 budget과 v1/v2는 보존한다. 범위·기대 결과·필수 검사 수를 줄이지 않는 보수적 시간 정정이며 연장/재설정이 아니다. 같은 실패 수정 상한 3회 및 old native D18 HELD는 유지한다.

**기존 코드 재사용/실현 가능성:** 계획의 binding `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`, journal `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`, parser `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`, ledger `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`, collector `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`, quota `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`가 실제 소스와 일치한다. public read_submission_binding/read_intent 및 기존 bounded JSON/SQLite validator로 로컬 단위를 구현할 수 있다. journal은 UNKNOWN revision>=1을 읽을 수 있으므로 새 모듈의 UNKNOWN AND revision1 단일 guard가 PREPARED0와 UNKNOWN2를 모두 거부해야 한다.

고정 PR16 transport `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415`의 call은 ID/오류 검증 뒤 decoded result를 반환한다. v2는 그 결과를 caller가 명시 직렬화한 bytes/hash만 받으므로 raw capture hook이나 기존 client 변경이 필요 없다. 이 transport는 새 모듈의 import/실행 의존성이 아니다. 기존 CLI help의 옵션 미표시는 native 불가능/가능 어느 쪽의 증명도 아니며 추가 help·IPC·runtime 조사는 필요하지 않다.

**API·입력·privacy:** initialize_queue_receipts/record_queue_receipt/read_queue_receipt 세 API는 경로·client·exception·clock 인자를 받지 않는다. strict builtin key/pin/bytes/code, 원문 hash 선검증, submission/ACK 각 cap와 합계 cap, PR19 decoder/text의 UTF-8·중복키·BOM·float/nonfinite·깊이·단일 text 제한이 명확하다. exact schema의 좁은 subset만 수용하는 것이며 generated schema 전체 지원을 주장하지 않는다. submission SHA는 C의 원문 SHA와 같고, intent prompt와 ACK text는 bytes/hash 그대로 대조한다. ACK client 단일 gate, identifier/type/shape 거부 및 valid mismatch의 ACK_MISMATCH 구분을 유지한다. P에는 검증된 ID·pin·outcome/reason만 저장하고 원문 prompt/params/serialized ACK·exception·wire frame은 저장/출력/cache하지 않는다.

**DB·source·상태:** record의 세 연결 구조/active 상태를 ANY SQL 전에 검사하고 profile은 BEGIN 전에 검증한다. 전용 file/DELETE/FULL 또는 EXTRA/timeout/factory/attachment 제한과 동일 객체·main filename 거부가 충분하다. 새 AQR1 DB는 빈 DB의 명시 초기화만 허용하고 schema 변경·repair·기존 source mutation은 없다. 두 public source read가 확정되고 transaction이 닫힌 뒤 receipt transaction을 시작한다. source pin·C·client/thread/params/prompt 검증과 최신 UNKNOWN1을 요구하지만 3DB atomic freshness나 owner 신뢰를 주장하지 않는다. P의 canonical bytes/hash·exact fields·column key·enum 관계를 저장/재조회 모두 검증한다.

첫 관찰 UNKNOWN도 immutable이다. 동일 P만 reuse하고 늦은 ACK/다른 code/직렬화/queued ID로 바꾸지 않으며 conflict 뒤 원래 bytes/hash/file을 보존한다. read는 receipt/context pins만 검증하는 로컬 조회다. UNKNOWN은 관찰 내용이고, SQLite UNCERTAIN은 저장 여부 불명이다. COMMIT 응답 유실은 row가 남아도 CHECKPOINT_UNCONFIRMED/record=None/new_receipt=false이며 명시적 read/reopen으로 확인한다. known invalid와 SQL 불명, source BLOCKED와 비정상/불명 결과, 소유 cleanup과 caller transaction 보존을 구분한 반환 계약이 적절하다. init/read의 source closed=null과 record의 invalid None=false도 명확하다.

**D06 경미 명확화 2 — fault 검사 구현(계획 85–87행):** 새 모듈의 _execute/_commit/_rollback 교체만으로 기존 journal._connection의 profile SQL에 도달했다고 세지 않는다. preflight SQL 주입은 실제 고정 journal._one(connection, sql) 두 인자 경계 또는 해당 SQLite authorizer 경계에서 수행하고 정확한 hit와 CHECKPOINT_UNCONFIRMED를 확인한다. BEGIN/INSERT/readback/commit/cleanup도 실제 경계 도달·전후 효과를 확인하며 arity/import 예외로 대체하지 않는다. 이는 이미 요구한 preflight SQL 불명 검사의 구현 위치 명확화이고 새 method나 기준 변경이 아니다. 'alias' 검사는 계획 28행대로 동일 객체/SQLite가 노출한 동일 main filename 거부를 뜻한다. hardlink/별칭 실체·소유자 식별은 caller 책임이며 이를 조사하려고 stat/경로/환경 IO를 추가하지 않는다.

**정상 4개/의미 변이 2개:** 제시한 네 method로 ACK 저장/reuse/reopen 및 다른 직렬화 conflict, UNKNOWN 최초 관찰 불변, source/pin/type/hash/shape/state/privacy/row 손상, 실제 temp file SQLite profile·소유 transaction·주입 실패/복구를 검증할 수 있다. 세 전용 임시 DB·peer·close/reopen과 첫 실행부터 상대 fixture/최소 source copy, strict IO guard를 요구한다. 새 method를 늘리지 않고 기존 네 method 안에서 공개 report의 정확한 keys/types/false flags·허용/거부·source 보존을 검증한다. 범위에 D30/D32/D35의 데이터/불명·중복/격리 검사가 포함되며 UI/LLM/부하·운영 migration/백업의 추가 실행은 필요하지 않다.

- M_CLIENT는 유효한 다른 ACK client만 만들고 단일 client 일치 guard만 True로 치환한다. ACK raw hash 자체는 정확해야 하며 '잘못된 ACK hash'는 다른 client의 ACK bytes를 원래 source와 잘못 결합한 의미다. 정상 helper의 ACK_MISMATCH/row0와 달리 실제 ACK_CANDIDATE/RECEIPT_STORED 및 잘못 결합된 P/hash의 peer·reopen commit witness가 있어야 한다.
- M_STATE는 UNKNOWN AND revision1 guard 전체만 True로 치환한다. 정상 PREPARED0 fixture에서 helper는 INTENT_NOT_UNKNOWN/row0이고 변이는 원래 PREPARED0를 보존한 채 실제 ACK receipt를 commit해야 한다. P builder에 중복 state gate를 넣어 변이를 가리지 않는다.

두 target의 정상 PASS는 세 번째 method 내 helper 각 1회에서 재사용한다. 정상 4개 모두 PASS/FAIL0/ERROR0/SKIP0 이후에만 각 변이를 지정 helper로 1회 실행한다. 다른 AST·파서·hash·commit·원본 source는 불변이며 지정 semantic assertion의 FAIL1/ERROR0/SKIP0, 실제 wrong P·bytes/hash·peer/reopen row·DB witness를 함께 요구한다. syntax/import/다른 guard/fixture ERROR는 성공이 아니다. 필수 수나 판정 의미를 완화하지 않는다.

**외부 경계/완료선:** 모든 remote/provenance/client/transport/demux/deadline/native/dispatch/replay flag는 false다. ACK_CANDIDATE나 durable UNKNOWN의 CONFIRMED는 원격 완료·native 인수가 아니다. 실제 caller의 bounded serialization/error adapter·source pins·소유 client/ID matching·기한·직렬화·live queue/ACK/controller·두 native 작업은 후속 구현/별도 권한 조건으로 남고 이번 독립 로컬 구현을 막지 않는다. 구현 후 SOURCE_READY 정적 검토, 새 4개/지정 2개 실제 근거와 최종 독립 검토를 별도로 받아야 한다.

이 검토는 파일 정적 조회와 이 보고서 작성만 수행했다. 코드·test·계획·DB·설정은 수정/실행하지 않았고 import/검사·RPC/socket/queue/credential/service·권한 조사·다른 agent 조회/호출은 없었다. 원본 11개 Git 상태와 기존 PR20 완료 기록·과거 결과를 보존했다. 기존 PASS/R03/R04/R15/이전 suite를 반복하지 않았으며 PAUSE·old native D18 HELD·운영/main/VMS 보호와 조사 상한을 유지한다.
