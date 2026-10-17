# QUEUE-ACK-20261017 · D29 frozen v1
상태 PLAN_COMPLETE / 독립 사전 검토 전. 이번에는 이 계획만 작성하며 제품·검사 작성/import/실행·SQL·native IO는0이다.
기준 PR20 `aef3af98d1bbc9037f1b75906e0217844bb5bd1d`, branch `codex/agent-project-queue-ack-20261017`; 실제 HEAD/clean 확인.
작업은 `var/unattended-20261015/ack-development`, 근거는 `transition-evidence/queue-ack-20261017` 아래에만 둔다.
`unit-budget.json`: 2026-10-17T01:52:09.392724Z→02:52:09.392724Z, 총60분·같은 실패 수정≤3·최초 오류 보존·시계 재설정0.
PR20 완료/readback232와 과거 코드/PASS/오류·원본11개를 보존한다. old native D18 HELD·scheduler/root/user PAUSE·사용량 보호는 그대로다.
`owner-cli-static-contract.json`의 help once는 caller-selected client ID/structured ACK를 제공한다고 입증하지 않았다. 재조회·실제 연결 조사는 금지한다.
새 socket/RPC/CLI queue·worker/서비스·권한/owner 설정·credential/운영/main/GitHub/VMS 조작은0, 역할은 총괄/test/reviewer만이다.

## 독립성·고정 재사용
의존성은 충족됐다. public binding read가 C/pins와 관찰 intent state/revision을 반환하므로 native 없이 새 receipt를 검증·저장할 수 있다.
새 제품은 `src/ops/tools/codex_unattended_queue_receipts.py` 하나, 새 검사는 `tests/test_codex_unattended_queue_receipts.py` 하나다.
PR20 binding SHA `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`의 public read_submission_binding을 호출한다.
PR18 journal SHA `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`의 key/hash/profile 검증을 재사용한다.
PR19 parser SHA `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`의 _inputs/_decode/_identifier/_text/_depth를 재사용한다.
ledger SHA `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`, collector SHA `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`의 bounded SQL/canonical/JSON hooks를 재사용한다.
既存 ThreadQueueAddResponse.json SHA `c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314`의 실제 bytes/hash를 확인했다. 상대 fixture로 정확히 복사한다.
기존 모듈/API/schema·operator script/receipt는 변경하지 않는다. immutable binding과 다른 최초 관찰을 별도 저장하므로 새 최소 schema가 필요하다.

## 공개 API·borrowed IO
`initialize_queue_receipts(receipt_conn) -> {report,record}`
`record_queue_receipt(intent_conn,binding_conn,receipt_conn,key,intent_pin,binding_pin,*,submission_params_bytes,submission_pin,request_bytes,request_pin,response_bytes_or_None,response_pin_or_None) -> {report,record}`
`read_queue_receipt(receipt_conn,key,intent_pin,binding_pin,receipt_pin) -> {report,record}`
key는 PR18 strict tuple3/agent_project, 모든 pin은 builtin str lower64hex다. path/client/clock/options 인자는 없다.
record는 서로 다른 전용 file DB3개, init/read는 receipt DB1개의 builtin sqlite3.Connection을 빌린다. 생성/close/context/reconnect/auto-init/migrate0.
record의 세 연결 모두 builtin 구조·closed·caller active TX를 ANY SQL 전에 검사한다. 같은 객체도 이 단계에서 거부한다.
그 뒤 journal._connection profile을 BEGIN 전에 검증: isolation=None/default factories, DELETE, FULL/EXTRA, busy_timeout 0..1000ms, memory/temp/attached 금지.
PRAGMA main filename이 같은 연결은 거부한다. alias/소유자/realpath 인증은 caller 책임이며 제품 stat/파일/환경/credential IO는0이다.
known 구조/profile 오류는 BLOCKED/CONNECTION_INVALID, profile SQL/분류 불명은 UNCERTAIN/CHECKPOINT_UNCONFIRMED다. caller TX를 종료하지 않는다.

## raw bytes·envelope·source 결합
strict builtin bytes 길이: submission 1..131072, request 1..196608, response 1..262144; 합≤589824. None response는 None pin과만 짝지어진다.
type/길이/pin 형식 확인 후 모든 raw SHA를 decode 전에 확인한다. mismatched SHA는 INPUT_HASH_MISMATCH, 잘못된 type/범위/pair는 ARGUMENTS_INVALID다.
submission/request는 PR19 decoder의 UTF-8/duplicate-key/BOM/float/nonfinite/depth≤8 제한을 유지한다. text는 UTF-8 1..16384bytes, ID는 ASCII identifier 1..128이다.
submission exact keys={threadId,clientUserMessageId,input}; single text 및 omitted/empty text_elements만 허용하고 PR19 _text로 얻은 UTF-8 bytes를 쓴다.
request exact keys={id,method,params}, optional jsonrpc는 builtin str "2.0"만. id는 builtin int 1..2^63-1(not bool), method는 "thread/queue/add"다.
request.params는 같은 strict submission shape이고 원래 parsed submission과 정확히 같다(키순서/공백 무관, omitted≠empty spans인 params는 다른 parsed object).
C의 submission_params_sha256은 submission 원문 SHA로 유지한다. serializer의 다른 request 원문/hash를 C의 params SHA로 대체하지 않는다.
public read_submission_binding(intent_conn,binding_conn,key,intent_pin,binding_pin)이 CONFIRMED/BINDING_OBSERVED여야 한다; blocked→SOURCE_BLOCKED, uncertain/비정상 결과→CHECKPOINT_UNCONFIRMED.
그 record의 raw C/hash를 기존 binding validator로 검증하고 C의 key/intent/binding pin·thread/client/params SHA가 입력과 같아야 한다; 아니면 BINDING_MISMATCH다.
관찰 report의 intent_state=="SUBMISSION_UNKNOWN" AND intent_revision==1이 단일 필수 guard다; PREPARED0/UNKNOWN2는 INTENT_NOT_UNKNOWN이다.
source read가 끝나 양쪽 source TX가 닫힌 뒤 receipt TX를 시작한다. source row/schema/profile을 바꾸지 않으며 3DB atomic freshness를 주장하지 않는다.
response None/None은 UNKNOWN/RESPONSE_ABSENT다. 응답은 exact {id,result} 또는 {id,error}, optional jsonrpc="2.0"만이다.
response id도 strict positive int64이며 request.id와 같아야 한다; 형식 오류 INPUT_INVALID, 다른 유효 id는 RPC_ID_MISMATCH다.
result exact {queuedSubmission}; queuedSubmission exact {id,clientUserMessageId,input}; queued id는 identifier, client는 C와 같고 _text(input)의 UTF-8는 submission text와 같아야 한다.
유효 result는 ACK_CANDIDATE/ACK_RESULT_OBSERVED다. 유효한 다른 client/text는 ACK_MISMATCH, shape/type/ID/parser 오류는 INPUT_INVALID다.
error exact {code,message} + optional data: code strict signed int32, message strict UTF-8 str 0..4096bytes. 유효하면 UNKNOWN/RPC_ERROR_OBSERVED이며 확정 실패가 아니다.
response 전용 작은 bounded decoder adapter는 collector _unique/_integer/_nonfinite 및 PR19 _depth를 재사용한다; finite float는 opaque error.data 안만 허용한다.
parse_float는 비유한/overflow를 거부하고, 정수 token≤20 chars·depth≤8·전체 response byte 상한을 유지한다. 그 밖의 필드는 위 strict type 검사로 float를 거부한다.
error data/message/code·예외·prompt·request/response/submission 원문은 projection/report/log/global cache/DB에 저장하거나 stringify하지 않는다.

## immutable projection·전용 schema
projection P exact keys: schema,project_id,batch_id,task_id,intent_sha256,binding_sha256,thread_id,client_user_message_id,submission_params_sha256,request_sha256,response_sha256,rpc_id,outcome,observation_reason,queued_submission_id.
schema=1; key/3pins/thread/client은 위 검증값; request/response SHA는 검증한 원문 pin, rpc_id는 request.id다. raw params/text/error는 필드가 없다.
ACK_CANDIDATE는 reason ACK_RESULT_OBSERVED·nonnull response SHA/queued ID; UNKNOWN은 queued ID=null이며 RESPONSE_ABSENT만 response SHA=null, RPC_ERROR_OBSERVED는 nonnull이다.
collector canonical(sorted/compact/ensure_ascii UTF-8/finite) P bytes≤4096와 SHA만 저장한다. 읽을 때 raw hash/canonical/exact keys/types/enum 관계/column key/context pins를 모두 재검증한다.
receipt application_id=0x41515231(AQR1), user_version=1. init만 empty sqlite_master+zero markers에서 BEGIN IMMEDIATE 후 아래 exact DDL/markers를 생성한다.
`CREATE TABLE queue_receipts (project_id TEXT NOT NULL, batch_id TEXT NOT NULL, task_id TEXT NOT NULL, receipt_bytes BLOB NOT NULL, receipt_sha256 TEXT NOT NULL, PRIMARY KEY(project_id,batch_id,task_id)) WITHOUT ROWID`
정상 DB의 재init도 DATABASE_NOT_EMPTY, foreign object/marker/DDL 불일치는 SCHEMA_INVALID; 자동 repair/migration/기존 schema 변경0이다.
record: 입력/source 검증→receipt BEGIN IMMEDIATE→schema/기존 row intrinsic 검증→P 원문 bytes 동일시 RECEIPT_REUSED, 다르면 RECEIPT_CONFLICT다.
최초 row가 없을 때만 INSERT→동일 TX bounded readback/hash/pin 확인→COMMIT와 실제 종료 확인 후 RECEIPT_STORED다. UPDATE/REPLACE/DELETE는 없다.
최초 UNKNOWN도 불변이다. 같은 projection만 reuse, 뒤늦은 ACK/다른 valid bytes/RPC ID/queued ID는 conflict·원래 row/hash/file 보존이며 재조회만 한다.
read는 receipt BEGIN→schema/row intrinsic/key/intent/binding/receipt pins 확인→COMMIT 종료; missing RECEIPT_NOT_FOUND, context/digest pin 불일치 RECEIPT_PIN_MISMATCH, 손상 ROW_INVALID다.
preflight/BEGIN/INSERT/readback/COMMIT/cleanup SQL 불명은 UNCERTAIN이다. BEGIN 전송 후 오류도 실제 in_transaction으로 owned TX만 rollback한다.
COMMIT 후 응답 유실에는 이미 row가 남아도 record=None/new_receipt=false로 반환한다. 자동 retry/re-record/re-init/삭제0; 명시적 read로만 확인한다.

## exact 반환 계약
항상 top keys={report,record}; record는 CONFIRMED stored/reused/observed만 exact {receipt_bytes:bytes,receipt_sha256:str}, init/blocked/uncertain은 None이다.
report exact keys={scope,status,reason,phase,database_commit_confirmed,new_receipt,outcome,source_binding_confirmed,source_reason,intent_transaction_closed,binding_transaction_closed,receipt_transaction_closed,remote_io_performed,remote_mutation_authorized,input_origin_verified,native_acceptance_verified,dispatch_permitted,replay_permitted,native_ready}.
scope="DURABLE_QUEUE_RECEIPT_ONLY"; status=CONFIRMED|BLOCKED|UNCERTAIN; database_commit_confirmed iff CONFIRMED, new_receipt iff RECEIPT_STORED다.
report.outcome은 confirmed record의 outcome만, 그 밖은 null이다. source_binding_confirmed는 public binding read의 confirmed record를 확인했을 때만 true다(권한/출처 증명 아님).
source_reason은 SOURCE_BLOCKED일 때 pinned binding의 고정 BLOCKED reason만, 그 밖 null; raw 예외/경로는 반환하지 않는다.
closed들은 실제 _closed bool이다. init/read는 source 인자가 없어 intent/binding closed=null, record API의 invalid None source는 bool false다.
remote_io_performed/remote_mutation_authorized/input_origin_verified/native_acceptance_verified/dispatch_permitted/replay_permitted/native_ready는 항상 false다.
CONFIRMED reasons={RECEIPTS_READY,RECEIPT_STORED,RECEIPT_REUSED,RECEIPT_OBSERVED}; UNKNOWN 저장도 CONFIRMED일 수 있으며 원격 성공이 아니다.
BLOCKED reasons={ARGUMENTS_INVALID,CONNECTION_INVALID,DATABASE_NOT_EMPTY,SCHEMA_INVALID,INPUT_HASH_MISMATCH,INPUT_INVALID,SOURCE_BLOCKED,INTENT_NOT_UNKNOWN,BINDING_MISMATCH,RPC_ID_MISMATCH,ACK_MISMATCH,RECEIPT_CONFLICT,RECEIPT_NOT_FOUND,RECEIPT_PIN_MISMATCH,ROW_INVALID}.
UNCERTAIN reason={CHECKPOINT_UNCONFIRMED}; phase={PRECHECK,INPUT_VALIDATE,SOURCE_READ,SOURCE_VALIDATE,RECEIPT_TRANSACTION,RECEIPT_VALIDATE,RECEIPT_WRITE,RECEIPT_READBACK,RECEIPT_COMMIT,RECEIPT_CLEANUP,COMPLETE}.
phase는 실제 실패 지점, cleanup 자체 실패는 RECEIPT_CLEANUP, 모든 CONFIRMED는 COMPLETE다. SQL 불명을 known BLOCKED로 축약하지 않는다.

## 최초 정상4 methods와 D30/D32/D35/D38 검증
새 TestCase는 unittest.TestCase만 상속한다. 아래 정확4 methods, synthetic fixture setup 함수만 기존 journal/binding APIs·support에서 재사용한다.
`test_ack_candidate_store_reuse_and_reopen`: 3 temp file DB, PREPARED binding→fixture claim UNKNOWN1→ACK store/peer 가시성/reuse row1/close-reopen exact P bytes/hash/pins; wire key순서/공백 허용과 raw params pin 유지.
`test_unknown_receipt_is_immutable`: 별도 key의 None/error(+finite data) UNKNOWN store/reuse/reopen; 늦은 valid ACK/error bytes 차이는 conflict, 최초 UNKNOWN/hash/양쪽 source 보존·replay=false.
`test_receipt_validation_conflict_and_privacy`: 모든 byte/hash/shape/id/client/text/params/source pin/state 경계·duplicate/BOM/nonfinite/depth/범위·valid 다른 projection conflict·저장 C/hash 손상; blocked row0 또는 기존 원본 불변.
이 method가 `case_response_id_mismatch_cannot_store_ack` 및 `case_prepared_intent_cannot_store_ack` helpers를 각각 한 번 호출하고 정상 target PASS를 기록한다.
`test_receipt_database_faults_and_profiles`: explicit empty init/reinit/schema/markers/alias/memory/temp/attached/None/closed/active TX before ANY SQL·profile 보존·caller未commit 보존; 소유 cleanup/close-reopen 누락0.
4번에는 preflight SQL·BEGIN 전후·INSERT 전후·readback·COMMIT 전후/cleanup 실패를 narrow _execute(conn,sql,parameters=()), _commit(conn), _rollback(conn)으로 주입하고 실제 hook hit/arity를 검증한다.
commit-after-loss는 UNCERTAIN/record=None 후 reopened read로 실제 P 보존 확인; source rows/bytes/hash/schema/profile 불변, 자동 재시도/remote calls0을 확인한다.
첫 정상4부터 다른 이름의 최소 source copy+상대 pinned schema/fixture를 사용한다. original checkout/opt/operator tmp 읽기·socket/subprocess/env/clock IO는 BaseException guard로 차단한다.
기존 strict_io/no_external_io/connect/profile/snapshot 등 함수만 재사용; PR20 최종 authorizer 해제는 Python3.10 호환 lambda *args: SQLITE_OK를 유지하고 old TestCase를 실행/상속하지 않는다.
정상4 failfast ONCE, FAIL/ERROR/SKIP이면 최초 raw log/source/test/fixture/환경/pins/관찰을 보존하고 중단한다. old PASS/R03/R04/R15/이전4/6/suite는 반복하지 않는다.
실제 검증은 stdlib SQLite3 file DB3개의 durable 저장/조회다. captured synthetic JSON은 native 송수신 인수가 아니며 UI/LLM/부하/운영 migration·백업복원은 영향 없어 추가하지 않는다.

## 정확2 semantic mutants · 정상4 전부 PASS 뒤 지정 helper만 한 번씩
M_ID: response["id"] == request["id"] 단일 비교만 True로 치환. target=case_response_id_mismatch_cannot_store_ack, 다른 유효 response ID 외 모든 source/bytes/hash/client/text는 정상이다.
정상은 BLOCKED/RPC_ID_MISMATCH·row0. mutant는 실제 RECEIPT_STORED/ACK_CANDIDATE·commit된 잘못된 row가 peer/reopen에서 보여야 하고 지정 semantic assertion FAIL1/ERROR0이어야 한다.
M_STATE: UNKNOWN AND revision1 guard 전체만 True로 치환. target=case_prepared_intent_cannot_store_ack, 유효 binding의 PREPARED0와 나머지 정상 ACK fixture를 쓴다.
정상은 BLOCKED/INTENT_NOT_UNKNOWN·row0. mutant는 PREPARED0를 보존한 채 실제 잘못된 ACK receipt commit이 있어야 semantic FAIL1/ERROR0이며 projection에 별도 state guard를 중복해 가리지 않는다.
둘 다 정상 helper PASS 재사용, 원본 source 불변·isolated copy만 변이·나머지 AST 동등 확인, 실제 wrong P/hash/peer row/DB witness와 assertion 위치를 보존한다. syntax/import/다른 guard/fixture ERROR는 검출 성공이 아니다.

## 순서·완료선·후속 외부 조건
지금 PLAN_COMPLETE/SHA 후 멈춤→총괄 frozen 사본·독립 D29 PASS→제품/검사 병행→SOURCE_READY/static 검토→최초4+조건부 정확2 mutants→최종 독립 검토/게시/readback 순서다.
모든 새 근거는 이 unit 아래 별도 run에 보존하며 최초 오류/기존 unit 근거를 덮지 않는다. 완료선은 새 module+정상4+지정2 semantic 검출+portable/fileSQLite 근거+최종 독립 PASS다.
외부 조건은 live caller/transport 담당자가 pin 확인된 client에서 caller ID를 유지한 request와 그 response의 bounded raw envelopes 및 출처/client identity를 제공하는 것이다; 현재 CLI help로 이 연결은 미확인이다.
actual send/ACK 원격 출처·claim→dispatch 직렬화·사용량/admission·owner authorization은 다음 별도 인수이며 이번 writer가 조사/설정/실행하지 않는다. ACK_CANDIDATE는 원격 완료·turn·결과 또는 replay 허가가 아니다.
