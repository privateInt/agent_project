# U01b1 · 전용 SQLite 제출 전 intent 저널 D29 계획

ID: **U01b1-D29-v1**. 상태: **독립 사전 검토 대기**.
이번 작성은 기존 소스·계획의 정적 조회와 이 문서 작성뿐이다.
제품/검사 구현·import·검사 실행·SQLite 연결/SQL 실행·runtime 조회는 하지 않았다.

## 독립성 판단과 범위

**독립 구현 가능. 이 저장 단계의 구현을 막는 미해결 외부 의존성은 없다.**
호출자가 이미 연 전용 임시 SQLite 연결에 immutable intent를 저장하고, 제출 시도 직전
단 한 번의 CAS claim을 기록하는 부품이다. 실제 RPC의 동작이나 서버 idempotence를 가정하지
않고 검증할 수 있다. owner의 실제 DB 경로/보호 설정, quota/control/PAUSE와 native 접근은
후속 통합의 조건이며 이번 임시 DB 구현의 전제는 아니다.

이 결론은 U01b 전체의 BLOCKED 상태를 해제하지 않는다. 원래 U01 계획의 reserve/ACK/queue/
terminal 전체를 함께 구현하지 않는다. 실제 queue/add/start/list, fake queue dispatch loop,
ACK/queue/turn ID 처리, worker/scheduler, settings 적용, 전역 state/PAUSE 변경은 제외한다.
네트워크·daemon·credential·운영 원본/DB/Docker·권한 probe도 없다.
**D18 runtime 조사 보류/누적 상한은 HELD 그대로이며 새 조사 예산을 부여하지 않는다.**

기존 PR #17 `86b8861ba5b3ea049b77937e6c00da73a4330377`의 최종 PASS/readback64와
PR #16 `8c1a9c18f496ad99311ad1e976c2e00c31817747`의 PASS/readback100은 사용자가 제공한
게시 기준이다. 이번에 GitHub를 다시 조회하거나 기존 검사를 실행하지 않았다.
실제로 읽은 로컬 기준:

| 대상 | SHA-256 |
|---|---|
| U01 전체 계획 | `57c43182b0ba07e1bb4ea0510e3b79b119fe6b761ea70cf58916c99cf8ef0d88` |
| U01 범위 축소 검토 | `498f7eda206c8b6f2c04b4d6626975f8383dfc441c1616a7ebc00d6d46aeca29` |
| admission-development `codex_unattended_admission.py` | `b012469262a0de2b9025b393ea2dadc6799933eb1a57fe10c68187809d85da51` |
| 같은 사본 `github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| 같은 사본 `github_fixture_controller.py` | `671885e474d4eb2eafe4f786ea8f7075924bbd96250b77ff832561e1b943c5e7` |
| 같은 사본 `github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |

## 재사용과 새 스키마가 필요한 이유

ledger의 빌린 연결 검증, SQL cursor 정리, BEGIN IMMEDIATE/소유 transaction rollback,
정확한 schema 검사, 원문 bytes+SHA 재검증, revision WHERE/rowcount CAS 패턴을 재사용한다.
controller의 영구 claim과 응답 불명 상태를 확정 실패로 바꾸지 않는 원칙도 유지한다.
canonical JSON은 기존 collector와 같은 sorted keys/compact separators/ensure_ascii 규칙이다.

기존 공개 ledger API는 고정 fixture assignment와 artifact 검증에, controller는
CLAIMED/PREPARATION_FAILED 및 두 fixture DB에 결합되어 있다. 저널의 prompt/settings digest,
client correlation, 제출 전 UNKNOWN 전이를 이 schema/report에 끼워 넣으면 fixture 계약을
바꾸게 된다. private `_run`도 해당 schema/result를 직접 호출하므로 범용 transaction API가
아니다. 기존 모듈을 변경하거나 그 검사를 다시 돌리지 않고, 작은 별도 schema와 같은
SQLite transaction 패턴을 구현한다. 공용 framework 추출이나 기존 DB migration은 하지 않는다.

후보 파일은 `src/ops/tools/codex_unattended_intent_journal.py`,
`src/ops/tools/tests/test_codex_unattended_intent_journal.py`와 명시적인 합성 fixture다.
사전 PASS 뒤 별도 `var/unattended-20261015/intent-development` 사본에서 구현한다.
이번에는 이 경로나 소스 파일을 만들지 않는다. Python 3.10 호환을 유지한다.

## 정확한 API와 immutable 입력

```python
initialize_intent_journal(connection) -> dict
reserve_intent(connection, expected_intent_sha256, intent_bytes) -> dict
read_intent(connection, key, expected_intent_sha256) -> dict
claim_intent(connection, key, expected_intent_sha256, expected_revision) -> dict
```

`key`는 정확히 builtin tuple `(project_id, batch_id, task_id)`다. API는 연결/경로/파일/clock을
만들거나 연결을 닫지 않는다. transaction 중 connection은 호출자가 다른 용도로 쓰지 않는다.
다른 connection을 이용한 경쟁은 지원한다. expected_revision은 bool을 제외한 0..2^63-2 int다.

`intent_bytes`는 1..16384 bytes의 아래 exact object B를 canonical JSON으로 인코딩한 원문이다.
canonical = UTF-8 `json.dumps(B, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
allow_nan=False)`. duplicate key/BOM/비canonical/type 오류는 거부한다.

```text
schema = 1 (strict int)
project_id = "agent_project"
repository, dev_root, git_common_dir
batch_id, task_id
project_sha256, task_sha256
base_sha, plan_sha256, prompt_sha256, settings_sha256
clientUserMessageId
```

repository는 `owner/agent_project` 형식, root/common dir는 절대 정규 문자열이다.
`.`/`..`/중복 slash/끝 slash/NUL/와일드카드를 받지 않는다. ID는 비어 있지 않은 128자 이하
ASCII `[A-Za-z0-9_.:-]+`다. base_sha는 lowercase40hex, 모든 `*_sha256`은 lowercase64hex다.
expected_intent_sha256은 B 원문 전체 SHA-256과 같아야 한다. 모든 필드는 저장 후 불변이다.

project_sha256은 B의 project_id/repository/dev_root/git_common_dir 네 필드 object의 canonical
hash와 일치해야 한다. task_sha256은 U01a의 exact task descriptor canonical hash,
plan_sha256은 승인 plan bytes hash, prompt_sha256은 정확한 제출 prompt UTF-8 bytes hash,
settings_sha256은 의도한 scoped settings object canonical hash다. 뒤 네 원문의 출처/일치는
trusted caller가 책임지고, 저널은 전달받은 digest의 형식·불변·결합만 검증한다.
raw prompt/settings/credential는 이 API가 받거나 저장하지 않는다.
clientUserMessageId는 caller가 미리 정해 전달한 correlation ID이며 DB가 생성/교체하지 않는다.
이 ID가 원격 서버의 idempotency key라는 가정은 없다.

## DB 경계와 최소 상태

caller/test setup만 fresh TemporaryDirectory 안의 새로운 파일 DB를 열고
`isolation_level=None`, row_factory=None, text_factory=str, DELETE journal,
synchronous FULL(2) 또는 EXTRA(3), busy_timeout 0..1000ms로 준비한다.
제품은 이를 조회/검증하며 PRAGMA 실행 설정을 자동 보정하지 않는다.
닫힌/다른 타입/기존 transaction/첨부 DB/temp object/인메모리 DB는 CONNECTION_INVALID다.
`main`은 파일 기반 한 개여야 한다. DB origin/owner/realpath 신뢰를 인증했다는 뜻은 아니다.
이 단계의 전용 경로 보장은 합성 테스트 setup의 책임이며 실제 배포 경로 인수는 별도다.

initialize만 BEGIN IMMEDIATE 안에서 **schema object 없음·application_id=0·user_version=0**을
확인한 뒤 schema를 만든다. 기존 정상 journal도 재initialize는 DATABASE_NOT_EMPTY로 거부한다.
다른 API는 초기화/수리/migration하지 않고 marker와 exact schema 불일치를 SCHEMA_INVALID로
거부한다. application_id=`0x41494A31`(AIJ1), user_version=1로 고정한다.

schema는 WITHOUT ROWID `submission_intents` 한 테이블과 그 UNIQUE 자동 index뿐이다.
컬럼은 `project_id, batch_id, task_id, client_user_message_id, intent_bytes,
intent_sha256, state, revision`이다. 첫 세 컬럼은 복합 PRIMARY KEY, correlation은 UNIQUE다.
state는 PREPARED/SUBMISSION_UNKNOWN만 허용한다. revision은 strict SQLite integer이며
0..2^63-1, PREPARED는 0, SUBMISSION_UNKNOWN은 1 이상이다. 정상 API의 유일한 전이는
PREPARED/0→SUBMISSION_UNKNOWN/1이다. 양의 revision에서도 UNKNOWN은 항상 재claim 불가다.
이 일반 revision 표현은 아래 M2가 다른 schema 제약에 가려지지 않고 상태 guard를 검증하게 한다.
예상 테이블/자동 index 외 table/view/trigger/index 또는 DDL 변경은 거부한다.

- reserve: 신규 B를 PREPARED/revision0으로 commit한다. 동일 key+동일 bytes/hash의 재요청은
  원래 row를 INTENT_REUSED로 돌려주며 기존 UNKNOWN도 그대로 둔다. 다른 bytes는
  INTENT_CONFLICT, 같은 correlation을 다른 key에 쓰면 CORRELATION_CONFLICT다.
  새 correlation으로 같은 key를 덮거나 새 row로 교체하지 않는다.
- read: key/pin을 확인하고 bounded blob 조회→raw hash→canonical/type→key/correlation/revision
  검증을 한다. READ transaction뿐이며 상태를 변경하지 않는다. 원래 bytes/hash를 반환한다.
- claim: BEGIN IMMEDIATE 안에서 pin을 검증한 뒤 단일 UPDATE의 **key+pin+expected revision+
  state=PREPARED** 조건으로 state=SUBMISSION_UNKNOWN, revision=revision+1을 기록한다.
  rowcount=1과 commit 확인 뒤에만 새 DB claim으로 보고한다. revision 불일치는
  REVISION_CONFLICT, revision이 일치하는 UNKNOWN의 재claim은 UNKNOWN_NO_REPLAY다.
  상태 검사는 해당 UPDATE predicate에서 강제하고 0-row 이유를 같은 transaction에서 판별한다.
  PREPARED 복귀·삭제·expiry·refund·재시도 API는 없다.

각 write는 원문/핀을 재조회하고 COMMIT의 성공 및 transaction 종료를 확인한다.
유효하지 않은 caller transaction을 commit/rollback하지 않는다. 자신이 시작한 transaction만
실제 in_transaction을 확인해 정리한다. lock/SQL/COMMIT/cleanup 응답이 불명확하면
CHECKPOINT_UNCONFIRMED이며 성공·확정 미실행으로 가장하지 않는다. 자동 retry/reconnect는 없다.

## 결과와 remote-IO 경계

반환 exact keys는 `{report, record}`다. report의 exact keys:

```text
scope="PRE_SUBMISSION_JOURNAL_ONLY"
status=CONFIRMED | BLOCKED | UNCERTAIN
reason
database_commit_confirmed: bool
transaction_closed: bool
new_claim: bool
remote_io_performed=false
remote_mutation_authorized=false
input_origin_verified=false
native_ready=false
```

CONFIRMED reasons: JOURNAL_READY, INTENT_STORED, INTENT_REUSED, INTENT_OBSERVED, CLAIM_RECORDED.
BLOCKED reasons: ARGUMENTS_INVALID, CONNECTION_INVALID, DATABASE_NOT_EMPTY, SCHEMA_INVALID,
INTENT_INVALID, INTENT_HASH_MISMATCH, INTENT_NOT_FOUND, INTENT_CONFLICT,
CORRELATION_CONFLICT, INTENT_PIN_MISMATCH, REVISION_CONFLICT, UNKNOWN_NO_REPLAY, ROW_INVALID.
UNCERTAIN reason: CHECKPOINT_UNCONFIRMED. CONFIRMED의 database_commit_confirmed만 true이며
new_claim은 **이번 호출에서 CAS와 commit을 확정한 CLAIM_RECORDED만** true다.
CONFIRMED는 SQLite 저장/관측의 확인이지 제출의 성공이 아니다.

record는 확인된 해당 row에 한해 위 8개 컬럼 이름을 key로 원래 bytes/digest/state/revision을
반환하며, initialize 또는 BLOCKED/UNCERTAIN에서는 None이다. report는 raw exception/경로/
상대 intent/client ID/prompt를 노출하지 않는다. transaction_closed는 실제 연결 상태를 반영한다.
고정 reason 외 문자열/print는 없다.

후속 adapter는 immutable intent commit와 새 UNKNOWN claim commit의 확인 **전에 원격 IO를
하면 안 된다**. 저장 오류/UNKNOWN을 이유로 다른 ID의 제출이나 자동 replay를 해서는 안 된다.
claim 성공 뒤 crash/응답 유실로 remote 접수 여부를 모르면 UNKNOWN을 남기고 조회만 해야 한다.
이 slice에는 remote callback 자체가 없으며 새 claim도 PAUSE/quota/실행 권한을 부여하지 않는다.
DB에서 PREPARED/row 없음이 관찰돼도 원격 미실행의 증거로 사용하지 않는다.
CHECKPOINT_UNCONFIRMED 뒤에는 observe만 하며 자동 재claim/재reserve/repair하지 않는다.
DB 장애 후 caller가 이 규칙을 지키는지, RPC bytes와 저장 digest가 실제로 일치하는지는 후속
adapter 인수다. 원격 exactly-once·서명/출처·OS 격리·전원 장애 복구를 이번 PASS로 주장하지 않는다.

## 새 검사: 정확히 5 methods

실제 stdlib SQLite 파일 DB와 caller-owned 연결을 사용한다. 운영 DB/기존 state를 열지 않는다.
setup에서만 connect/PRAGMA 설정/close를 수행한다. 제품 호출 중 connect와 별도 파일·clock·
socket/subprocess/queue API를 차단하고 stdout/stderr0을 확인한다. borrowed 연결은 호출 뒤에도
실제 SQL 사용 가능해야 하며, close/connection 생성이 없는 것은 소스 대조로도 확인한다.
기존 fixture ledger/controller,
U01a 6개+변이2개, transport 통과 검사는 재실행하지 않는다.

| method | 입력과 반드시 확인할 결과 |
|---|---|
| test_empty_initialize_and_borrowed_connection | 새 전용 file DB만 explicit initialize 성공. 재initialize/다른 object·app/version는 bytes/logical row/marker를 변경하지 않고 거부. implicit init/migration0. 기존 caller transaction/잘못된 factory/attached/temp/memory/낮은 synchronous 거부, caller transaction·연결 설정·연결 소유권 보존 |
| test_immutable_reserve_roundtrip_and_conflicts | 합성 B reserve→다른 connection 가시성→close/reopen에서 모든 원문 bytes/digest/client ID/rev0 동일. exact replay는 기존 row만 재사용. 같은 key의 project metadata/base/plan/task digest/prompt/settings/correlation 변경과 다른 key의 correlation 재사용은 각각 고정 충돌; 원래 row 보존. 충돌 입력도 canonical/hash/project digest가 유효하도록 구성해 다른 검증이 가리지 않음. hash/type/key/duplicate/noncanonical/oversize·stored bytes/hash 손상은 고정 거부와 공개 비노출 |
| test_stale_revision_rejected | **단일 M1 target**: 정상 PREPARED/rev0, 맞는 key/pin, expected_revision=1만 불일치. REVISION_CONFLICT/new_claim=false/record=None, 별도 readback에서 원래 state/rev0/bytes 보존 |
| test_unknown_never_rearmed | **단일 M2 target**: setup에서 원래 intent의 정상 claim commit 후 close/reopen, 같은 key/pin와 expected_revision=1. UNKNOWN_NO_REPLAY/new_claim=false; readback UNKNOWN/rev1/모든 ID·bytes 보존. 다음 의도를 생성하거나 RPC를 보내지 않음 |
| test_claim_commit_boundary_reopen_and_failures | 정상 claim은 commit 전 외부 가시성/새 claim 보고 없음, commit 뒤 UNKNOWN/rev1 가시성 및 재open 원문 보존. 이 상태의 동일 reserve는 INTENT_REUSED지만 UNKNOWN/rev1 유지·새 claim false. 두 실제 connection의 경쟁은 winner1, loser REVISION_CONFLICT이며 변경1회. BEGIN/SQL/COMMIT 전 오류와 실제 COMMIT 뒤 응답 유실을 좁은 내부 hook으로 주입: CHECKPOINT_UNCONFIRMED·새 claim false·추정 실패/자동 재시도0. 후자의 재open은 동일 UNKNOWN/rev1이며 신규 claim 불가. 손상/미확인 schema는 fail closed, 소유 transaction만 정리 |

COMMIT 뒤 응답 유실은 real COMMIT 후 exception을 내는 test hook이며 전원 장애 실험이 아니다.
본 제품이 수행하는 SQL 저장은 D30의 실제 SQLite 인수다. remote 연결/실제 queued 작업은 NOT_RUN이다.

## D38: 지정 2개 isolated mutants만

최초 정상 5개가 모두 FAIL/ERROR/SKIP0인 경우에만 별도 source 사본에서 진행한다.
단일 target의 정상 최초 PASS를 재사용하고 normal이나 다른 target을 반복하지 않는다.

1. **M1 / test_stale_revision_rejected**: claim UPDATE에서 expected revision predicate와 그
   대응 SQL parameter만 제거한다. key/pin/state/commit 검사는 유지한다. 실제 rev0인데
   expected1이 통과해 CLAIM_RECORDED/new_claim=true/UNKNOWN rev1이 되므로 semantic
   **FAIL1/ERROR0**이어야 한다. schema/import 오류나 다른 guard의 차단은 검출 성공이 아니다.
2. **M2 / test_unknown_never_rearmed**: 같은 UPDATE의 state=PREPARED predicate만 제거한다.
   맞는 revision1은 유지하므로 UNKNOWN row를 rev2로 갱신하고 잘못된 새 claim을 보고한다.
   이 원래 UNKNOWN의 재arm을 target이 semantic **FAIL1/ERROR0**으로 검출해야 한다.
   real RPC/실제 두 번째·세 번째 task는 없고 schema/fixture/기대 reason을 바꾸지 않는다.

## 실행/보존/완료선

이 exact plan의 독립 사전 PASS 뒤에만 제품/검사를 작성한다. 지금 구현·실행하지 않는다.
새 5개를 최초 한 번만 실행하고 최초 실패/오류가 있으면 보존 후 중단한다. 기대값을 제품에
맞춰 바꾸거나 전체/기존 검사를 재실행하지 않는다. 변이 원문/diff/log/result는 정상과 별도다.
source/test/합성 B/plan/review/hash·환경·최초 log/결과·SQLite 저장/재open 근거를
원 저장소 `var/unattended-20261015/transition-evidence/intent-run-1` 아래 새로 보존한다.
기존 결과/오류와 원래 미커밋 11개는 그대로 둔다.

완료선: 새5개 PASS·지정2개 의미 FAIL1/ERROR0·원문/ID/claim 불변·최종 독립 검토·근거 보존.
후속 native 계약·owner 설정·U01a↔journal↔transport 연결·ACK/결과/게시 검증·실제 두 작업은
별도 미완료다. 이번 계획/오프라인 PASS로 U01b 전체나 현재 native runtime을 준비 완료로
표시하지 않는다. D18 HELD를 해제하거나 다른 agent를 조회/통지하지 않는다.
