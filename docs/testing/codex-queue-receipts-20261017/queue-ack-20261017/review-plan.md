# QUEUE-ACK-20261017 · D29 frozen v2 · CURRENT
상태 PLAN_COMPLETE_CURRENT / 독립 D29 PASS 전. 이번에는 이 파일만 작성하며 제품·검사 작성/import/실행·SQL·native IO는0이다.
v1 SHA `4f5785120859d3bbc7d5ee306e6b616f44d4a9e195bb24e7fc69612f7d34f122`는 역사로 보존하고 구현 기준은 이 v2 하나다.
변경 이유: 기존 caller-owned client.call은 ID를 내부 검증한 decoded result만 반환하고 error body를 버린다. 존재하지 않는 raw capture hook을 요구하지 않는다.
request bytes/pin·RPC ID·envelope·error body adapter를 삭제한다. 아래 hash는 caller가 명시 serialize한 decoded result의 hash이며 wire response hash가 아니다.
기준 PR20 `aef3af98d1bbc9037f1b75906e0217844bb5bd1d`, branch `codex/agent-project-queue-ack-20261017`, work `var/unattended-20261015/ack-development`다.
unit-budget: 2026-10-17T01:52:09.392724Z→02:52:09.392724Z, 총60분·같은 실패 수정≤3·최초 오류 보존·시계 재설정0. steering 시간도 포함한다.
PR20/readback232·기존 코드/검사/PASS/최초 오류·원본11개·old native D18 HELD·scheduler/root/user PAUSE·사용량/운영 보호는 불변이다.
새 RPC/socket/CLI queue/help 재실행·실제 연결 조사·worker/서비스/owner/권한/credential/production/main/GitHub/VMS 조작은0, 역할은 총괄/test/reviewer다.

## 재사용·새 파일
새 module `src/ops/tools/codex_unattended_queue_receipts.py`, 새 test `src/ops/tools/tests/test_codex_unattended_queue_receipts.py`만 추가한다.
PR20 binding `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c` public read_submission_binding과 canonical C validator를 재사용한다.
PR18 journal `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`의 public read_intent/key/hash/profile/intent validator를 재사용한다.
PR19 parser `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`의 _inputs/_raw/_decode/_identifier/_text를 재사용하고 protocol decoder를 새로 만들지 않는다.
ledger `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`, collector `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`의 bounded SQL/canonical을 재사용한다.
quota reader `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`의 고정 _ERRORS를 참조만 하며 reader/client를 호출하지 않는다.
ThreadQueueAddResponse.json `c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314`는 상대 schema fixture로 정확 bytes/hash 복사한다.
기존 모듈/API/schema/operator script/receipt는 수정하지 않는다. 별도 최초 관찰 불변 원장을 위한 최소 새 schema만 필요하며 offline 구현 의존성은 충족됐다.

## 공개 API·borrowed 경계
`initialize_queue_receipts(receipt_conn) -> {report,record}`
`record_queue_receipt(intent_conn,binding_conn,receipt_conn,key,intent_pin,binding_pin,*,submission_params_bytes,expected_submission_sha256,ack_result_bytes=None,expected_ack_sha256=None,call_error_code=None) -> {report,record}`
`read_queue_receipt(receipt_conn,key,intent_pin,binding_pin,receipt_pin) -> {report,record}`
key는 PR18 strict tuple3/agent_project, pin은 builtin str lower64hex다. client/path/clock/transport/options·원본 exception 인자는 없다.
record는 서로 다른 전용 file DB3개, init/read는 receipt DB1개의 builtin sqlite3.Connection을 빌리며 연결 생성/close/context/reconnect/auto-init/migrate0이다.
record의 세 연결 모두 builtin 구조·closed·caller active TX·같은 객체 여부를 ANY SQL 전에 검사한다. profile은 BEGIN 전 journal._connection을 재사용한다.
isolation=None/default factories·DELETE·FULL/EXTRA·busy_timeout 0..1000ms만 허용, memory/temp/attached·같은 main filename은 CONNECTION_INVALID다. alias/소유자 인증은 caller 책임이다.
known 구조/profile invalid는 BLOCKED/CONNECTION_INVALID, preflight SQL/분류 불명은 UNCERTAIN/CHECKPOINT_UNCONFIRMED다. caller TX 종료/보정·stat/환경/credential IO는0이다.

## 입력·decoded result·source 검증
submission은 strict bytes 1..131072, ack result는 strict bytes 1..262144, 합≤393216이다. 모든 byte/pin type·길이·조합 확인 후 raw SHA를 decode 전에 검증한다.
ack bytes가 있으면 expected_ack_sha256 필수·call_error_code=None, 없으면 ack/pin 둘 다 None이어야 한다. 잘못된 조합/type/범위/pin 형식은 ARGUMENTS_INVALID다.
call_error_code는 None 또는 builtin str로 quota_reader._ERRORS와 UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR 중 하나만 받는다. type 확인이 equality/membership보다 먼저이며 다른 값/exception은 ARGUMENTS_INVALID다.
caller가 안전한 code를 선택한다. 제품은 exception 속성/메시지/args/str/repr를 조사하지 않고 unknown raw code를 보관하지 않는다.
raw SHA 불일치는 INPUT_HASH_MISMATCH. PR19 strict UTF-8/duplicate-key/BOM/float/nonfinite/depth≤8 제한을 그대로 적용하며 malformed/unsupported shape는 INPUT_INVALID다.
submission exact {threadId,clientUserMessageId,input}, identifier ASCII 1..128, single text UTF-8 1..16384bytes·omitted/empty text_elements만 지원한다.
expected_submission_sha256은 제출 원문 bytes의 SHA이며 C.submission_params_sha256과 그대로 같아야 한다; ack 직렬화/정규화 hash로 대체하지 않는다.
public read_submission_binding이 CONFIRMED/BINDING_OBSERVED, 이어 public read_intent가 CONFIRMED/INTENT_OBSERVED여야 한다. 알려진 source BLOCKED→SOURCE_BLOCKED, uncertain/비정상 결과→CHECKPOINT_UNCONFIRMED다.
기존 validators로 C/intent raw bytes와 pins를 확인하고 key·intent/binding pin·C thread/client/params SHA·원래 intent client를 대조한다; 불일치는 BINDING_MISMATCH다.
latest read_intent record의 state==SUBMISSION_UNKNOWN AND revision==1이 단일 필수 guard다. PREPARED0/UNKNOWN2는 INTENT_NOT_UNKNOWN이며 별도 중복 state gate는 두지 않는다.
SHA256(PR19 _text(submission.input))==intent.prompt_sha256을 필수로 확인한다. ACK text도 같은 SHA와 UTF-8 bytes를 대조하되 Unicode/줄바꿈 정규화는 하지 않는다.
source 두 TX가 닫힌 뒤 receipt TX를 시작한다. 양쪽 source rows/schema/bytes/hash/profile은 불변이며 3DB atomic freshness·실제 caller identity를 증명하지 않는다.
ack_result_bytes는 decoded ThreadQueueAddResponse object의 명시적 serialization이며 exact {queuedSubmission}, queuedSubmission exact {id,clientUserMessageId,input}만 받는다.
queued id는 identifier, client는 위 검증한 원래 client와 한 번의 필수 gate로 같아야 하며 _text(input)는 submission UTF-8/hash와 같아야 한다.
유효 ACK는 ACK_CANDIDATE/ACK_RESULT_OBSERVED. 유효한 다른 client/text는 ACK_MISMATCH, 잘못된 ID/shape/type/parser는 INPUT_INVALID다. RPC wrapper/error body/id는 입력 계약에 없다.
ack None/pin None/code None은 UNKNOWN/ACK_RESULT_ABSENT; 허용 code가 있으면 UNKNOWN/그 code 자체를 observation_reason으로 저장한다. 모두 확정 원격 실패가 아니다.
제출/ACK serialized result/원래 prompt·exception·wire frame 원문은 DB/report/log/global cache에 보관하지 않는다. 제품은 serialize나 native 호출을 수행하지 않는다.

## 최초 관찰 불변 projection·schema
P exact keys={schema,project_id,batch_id,task_id,intent_sha256,binding_sha256,thread_id,client_user_message_id,submission_params_sha256,ack_result_sha256,outcome,observation_reason,queued_submission_id}.
schema=1; key/pins/thread/client는 검증 source, ack_result_sha256은 serialized decoded result SHA 또는 null이다. request/response hash·RPC ID 필드는 없다.
ACK_CANDIDATE는 nonnull ack hash/queued ID·reason ACK_RESULT_OBSERVED, UNKNOWN은 hash/queued ID 둘 다 null·reason ACK_RESULT_ABSENT 또는 허용 error code다.
collector canonical(sorted/compact/ensure_ascii UTF-8/finite) P bytes≤4096와 SHA만 저장한다. readback은 raw hash/canonical/exact keys/types/enum 관계/column key/context pins를 재검증한다.
receipt application_id=0x41515231(AQR1), user_version=1. init만 empty sqlite_master+zero markers의 BEGIN IMMEDIATE에서 exact DDL/markers를 만든다.
`CREATE TABLE queue_receipts (project_id TEXT NOT NULL, batch_id TEXT NOT NULL, task_id TEXT NOT NULL, receipt_bytes BLOB NOT NULL, receipt_sha256 TEXT NOT NULL, PRIMARY KEY(project_id,batch_id,task_id)) WITHOUT ROWID`
재init은 DATABASE_NOT_EMPTY, foreign object/marker/DDL 불일치는 SCHEMA_INVALID; migration/repair/기존 source schema 변경0이다.
record: 입력/source 검증→receipt BEGIN IMMEDIATE→schema/기존 row intrinsic 검증→P 원문 bytes 동일만 RECEIPT_REUSED, 다른 valid P는 RECEIPT_CONFLICT다.
최초 row 없음일 때만 INSERT→같은 TX bounded readback/hash/pins 확인→COMMIT와 실제 종료 확인 후 RECEIPT_STORED. UPDATE/REPLACE/DELETE는 없다.
최초 UNKNOWN도 불변: 뒤늦은 ACK/다른 code/다른 유효 serialized ack bytes 또는 queued ID는 conflict·원본 row/hash/file 보존이며 재조회만 한다.
read: receipt BEGIN→schema/row/key/intent/binding/receipt pins 검증→COMMIT 종료; missing RECEIPT_NOT_FOUND, context/digest pin mismatch RECEIPT_PIN_MISMATCH, 손상 ROW_INVALID다.
preflight/BEGIN/INSERT/readback/COMMIT/cleanup SQL 불명은 UNCERTAIN. BEGIN 실행 뒤 오류도 실제 in_transaction으로 owned TX만 cleanup하며 자동 retry/record/init/삭제0이다.
COMMIT 후 응답 유실에는 row가 남아도 record=None/new_receipt=false로 반환하고 명시적 read/reopen으로만 확인한다. UNKNOWN 관찰과 SQLite UNCERTAIN은 다른 축이다.

## exact 반환·외부 책임
항상 top keys={report,record}; record는 CONFIRMED stored/reused/observed만 exact {receipt_bytes:bytes,receipt_sha256:str}; init/BLOCKED/UNCERTAIN은 None이다.
report exact keys={scope,status,reason,phase,database_commit_confirmed,new_receipt,outcome,source_binding_confirmed,source_reason,intent_transaction_closed,binding_transaction_closed,receipt_transaction_closed,remote_io_performed,remote_mutation_authorized,input_origin_verified,client_identity_verified,transport_pin_verified,transport_id_matching_verified,deadline_verified,native_acceptance_verified,dispatch_permitted,replay_permitted,native_ready}.
scope=DURABLE_QUEUE_RECEIPT_ONLY; status=CONFIRMED|BLOCKED|UNCERTAIN; database_commit_confirmed iff CONFIRMED, new_receipt iff RECEIPT_STORED; outcome은 confirmed record에만 있고 그 밖 null이다.
source_binding_confirmed는 public binding read의 confirmed record 확인만 뜻한다. source_reason은 SOURCE_BLOCKED일 때 pinned binding/journal 고정 BLOCKED reason만, 그 밖 null이다.
closed는 실제 _closed bool; init/read의 source closed는 인자가 없어 null, record의 invalid None source는 bool false다. 성공 phase=COMPLETE, 실패 phase=실제 단계, cleanup 자체 실패=RECEIPT_CLEANUP이다.
remote_io_performed/remote_mutation_authorized/input_origin_verified/client_identity_verified/transport_pin_verified/transport_id_matching_verified/deadline_verified/native_acceptance_verified/dispatch_permitted/replay_permitted/native_ready는 항상 false다.
CONFIRMED reasons={RECEIPTS_READY,RECEIPT_STORED,RECEIPT_REUSED,RECEIPT_OBSERVED}; UNKNOWN을 durable 저장한 CONFIRMED는 원격 성공/실패 확정이 아니다.
BLOCKED reasons={ARGUMENTS_INVALID,CONNECTION_INVALID,DATABASE_NOT_EMPTY,SCHEMA_INVALID,INPUT_HASH_MISMATCH,INPUT_INVALID,SOURCE_BLOCKED,INTENT_NOT_UNKNOWN,BINDING_MISMATCH,ACK_MISMATCH,RECEIPT_CONFLICT,RECEIPT_NOT_FOUND,RECEIPT_PIN_MISMATCH,ROW_INVALID}.
UNCERTAIN reason={CHECKPOINT_UNCONFIRMED}; phase={PRECHECK,INPUT_VALIDATE,SOURCE_READ,SOURCE_VALIDATE,RECEIPT_TRANSACTION,RECEIPT_VALIDATE,RECEIPT_WRITE,RECEIPT_READBACK,RECEIPT_COMMIT,RECEIPT_CLEANUP,COMPLETE}.
실제 client 선택/소유권·source pin·transport ID matching/deadline/demultiplexing·원격 출처·claim→dispatch 직렬화·사용량/admission은 외부 caller 책임이다. 이 module은 이를 검증했다고 표시하지 않는다.
기존 client 변경/raw capture hook은 필요 없다. live caller가 decoded result만 bounded serialize하고 안전한 code를 전달하는 adapter와 실제 연결 인수는 후속 작업이다; 현재 CLI help로 그 연결은 미확인이다.
ACK_CANDIDATE는 원격 완료·turn·작업 결과 또는 dispatch/replay 허가가 아니다. native 입력/출력은0이다.

## 원래 정상4 methods · portable/file SQLite
새 TestCase는 unittest.TestCase만 상속하며 기존 journal/binding APIs와 support의 setup 함수만 재사용한다. 첫 실행부터 최소 source copy·상대 pinned schema/fixture를 쓴다.
`test_ack_candidate_store_reuse_and_reopen`: 3 temp file DB, PREPARED binding→fixture claim UNKNOWN1→ACK store/peer/reuse/reopen exact P/hash/pins; decoded result의 다른 유효 직렬화는 다른 hash로 conflict한다.
`test_unknown_receipt_is_immutable`: None/None 및 허용 code·UNCLASSIFIED UNKNOWN store/reuse/reopen, 늦은 ACK/다른 code conflict, 최초 UNKNOWN/source/hash 보존·권한 flags=false다.
`test_receipt_validation_conflict_and_privacy`: input pair/strict code/type/hash/bytes/shape/queued ID/client/text/params/intent pins/state·duplicate/BOM/float/nonfinite/depth/bounds·conflict/row corruption·privacy를 확인한다.
그 method는 `case_ack_client_mismatch_cannot_store`와 `case_prepared_intent_cannot_store_ack` helpers를 각각 한 번 호출해 정상 target PASS를 기록한다.
`test_receipt_database_faults_and_profiles`: empty init/reinit/schema/marker/alias/memory/temp/attached/None/closed/active TX before SQL·caller 未commit/profile/원본 보존·교체된 현재 연결 identity-dedup cleanup을 확인한다.
4번의 narrow _execute(conn,sql,parameters=()), _commit(conn), _rollback(conn) hooks는 preflight SQL/BEGIN 전후/INSERT 전후/readback/COMMIT 전후/cleanup 실패의 실제 hit/arity를 검증한다.
commit-after-loss는 UNCERTAIN/record=None 뒤 reopened read로 P 확인, source bytes/rows/hash/schema/profile 보존·autoretry0. authorizer reset은 Python3.10 lambda *args: SQLITE_OK다.
기존 strict_io/no_external_io/connect/profile/snapshot 함수만 재사용하고 original checkout/opt/operator tmp·socket/subprocess/env/clock 접근을 BaseException guard로 차단한다.
정상4 ONCE failfast; FAIL/ERROR/SKIP은 최초 source/test/fixture/schema/plan/review/env/pins/raw log/관찰을 보존하고 중단한다. 기존 PASS/R03/R04/R15/이전4/6/suite는 반복하지 않는다.
D30/D32/D35는 실제 SQLite file DB3개 durable/충돌/오류/격리로 확인하며 synthetic decoded result는 native 인수와 구분한다. UI/LLM/부하/운영 migration·백업복원은 영향 없어 추가하지 않는다.

## 정확2 semantic mutants · 정상4 전부 PASS 뒤 지정 helper만 ONCE
M_CLIENT: ACK client==검증된 원래 client 단일 gate만 True로 치환; target=case_ack_client_mismatch_cannot_store. 다른 유효 client만 바꾸고 params/source/ACK hash/text/ID는 모두 유효하게 둔다.
정상은 BLOCKED/ACK_MISMATCH·row0; mutant는 실제 RECEIPT_STORED/ACK_CANDIDATE·잘못된 ack_result_sha256을 가진 commit row가 peer/reopen에서 보여야 지정 semantic assertion FAIL1/ERROR0이다.
M_STATE: UNKNOWN AND revision1 guard 전체만 True로 치환; target=case_prepared_intent_cannot_store_ack. 유효 PREPARED0 binding/intent와 정상 serialized ACK fixture를 쓴다.
정상은 BLOCKED/INTENT_NOT_UNKNOWN·row0; mutant는 PREPARED0를 보존하며 잘못된 ACK receipt commit이 있어야 semantic FAIL1/ERROR0이다. builder에는 state gate를 중복하지 않는다.
두 변이는 P.client를 원래 bound client로 유지하고 나머지 AST/파서/hash/commit 동등·원본 source 불변이다. 정상 helper PASS 재사용, 실제 wrong P/hash/peer/reopen row/DB witness·정확 assertion 위치를 보존한다.
syntax/import/다른 guard/fixture ERROR는 검출 성공이 아니다. 지금 PLAN_COMPLETE_CURRENT/SHA 후 멈춤→총괄 frozen v2/D29 PASS→병행 구현→SOURCE_READY/static review→최초4+조건부2→최종 독립 검토/게시/readback이다.
근거는 이 unit의 별도 run에 보존한다. 완료선은 새 module+정상4+지정2 semantic 검출+portable/fileSQLite 근거+최종 독립 PASS이며 native 연결 완료를 뜻하지 않는다.
