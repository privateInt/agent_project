# OWNER-BINDING-20261017-D29-v1

상태: **PLAN_COMPLETE / 독립 사전 검토 대기**. D29 계획만 작성했다.
규칙 기준은 저장소 `docs/codex/common/TESTING.md` D29이며, 관련 D30/D32/D35/D38을 연결한다.
제품·검사 구현/import/실행·SQLite 연결/SQL·네트워크/socket/RPC/queue 호출은 하지 않았다.
총괄이 이 문서의 frozen 사본과 reviewer 사전 PASS를 확보한 뒤 구현/검사 작성을 배정한다.
이번 계획의 작성자가 다른 agent를 조회하거나 위임하지 않는다. 역할은 총괄·test_agent·reviewer뿐이다.

## 기준·시간·보존

작업 공간은 `var/unattended-20261015/binding-development`, 기준은 PR19
**`ddec1931eed8828cdd84ae783845729edf617fc1`**이다. HEAD/미커밋 변경 없음과 아래 제품
worktree bytes/commit bytes의 일치를 정적으로 확인했다. Python 3.10을 유지한다.

| 재사용 대상 | SHA-256 |
|---|---|
| `codex_unattended_intent_journal.py` / PR18 intent | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| `codex_unattended_queue_observer.py` / PR19 parser | `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a` |
| `github_fixture_ledger.py` / SQLite primitives | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `github_fixture_collector.py` / canonical JSON helpers | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |

새 단위 전체 시계는 **2026-10-17T00:57:31Z → 01:57:31Z, 최대60분**이다. 계획·review·
구현·검증을 포함한다. 재개/역할/파일을 나누어 시계를 재설정하지 않는다. 같은 실패의 fix는
최대3회이며 최초 실패를 보존하고 해당 단계만 수정·재검증한다. 상한 이후 자동 반복하지 않는다.
기존 native D18 조사 보류·누적 상한과 scheduler/root/user PAUSE는 그대로다.

사용자가 알린 새 owner 사용량 관찰을 보존 receipt와 대조했다. `quota-read-once-20261017.json`은
2026-10-17 00:57UTC의 initialize + account/rateLimits/read **2RPC·0retry**, initialized
notification1, ordinaryUsageAllowed=true, weekly usedPercent14를 기록한다. 이것은 이미 끝난
owner 실행의 근거이며 이번 작성자의 신규 RPC 결과가 아니다. 과거 ordinary=false/100% 차단,
owner metadata의 max6RPC workflow와 미완료 thread/resume은 삭제·성공 처리하지 않는다.
**기존 owner sixRPC와 thread/resume을 재실행하지 않는다.** 최신 사용량은 D18/PAUSE 해제를 뜻하지 않는다.
이 정적 조회로 과거 owner 오류의 원인을 확정하지 않는다. A는 향후 로컬 reader의 오류 code/phase
보존을 보완하며 기존 owner 실패가 해결됐거나 resume을 완료했다는 판정이 아니다.

| 그대로 보존할 기존 원문 | SHA-256 |
|---|---|
| `owner-native-metadata-once.json` | `22e14deebb25db329fb0ebef6b22659167d2ffe06d3cbaeddd90b2fbc6a430bc` |
| `owner_metadata_once.py.txt` | `7ec43e502ed7a59e61c3abfd8949df059fb9dbb2f80657fdcd93dee08f83d259` |
| `quota-read-once-20261017.json` | `57a25a332bf29dfe3eabaee2817a1be5d75dd8114177b6a82bac8d51a9e0825c` |
| `../quota_read_once_20261017.py` | `42ff17b939e4fe81b0e6d8eb1c86ed406b16acdc805d7fbc55f9b7ecfeccfdc2` |

원본11개 미커밋 파일 hash도 기존 보존 값과 같다. 새 모듈은 다음 두 개뿐이다.

- A: `src/ops/tools/codex_unattended_quota_reader.py`
- B: `src/ops/tools/codex_unattended_submission_binding.py`

기존 실행 script/receipt, transport, intent/observer/ledger/schema를 수정하지 않는다.
기존 R03/R04/R15·U01a·U01b1·PR19의 4개 통과 검사와 전체 suite는 반복하지 않는다.

## A · 주입된 owner client를 쓰는 quota-only reader

### 공개 API와 호출 경계

```python
read_quota_only(client) -> dict
```

caller가 이미 소유한 **연결됐고 initialize 가능한 client**를 빌린다. client는
`call(method, params) -> decoded result`와 `notify_initialized() -> None`을 제공한다.
caller가 native response/error demultiplexing·decode 상한·wire/시간 상한·transport source pin을
책임진다. reader는 client factory/endpoint/path/credential/options를 받지 않으며, context 진입/
종료·close/재연결·socket·subprocess·thread·queue·services·settings를 직접 다루지 않는다.
실제 transport를 import하지 않는다. 기존 client/transport pin을 확인했다고 스스로 표시하지 않는다.

메서드를 미리 확인한 뒤 아래 순서만 허용한다. 초기화된 다른 세션을 탐지·재개·재설정하지 않는다.

1. `client.call("initialize", {"clientInfo":{"name":"agent_project_quota_reader","version":"1"},
   "capabilities":{"experimentalApi":true}})` 한 번. 결과가 builtin dict가 아니면 중단한다.
   initialize 결과의 private metadata는 읽어 저장하거나 반환하지 않는다. 전체 native schema
   인증을 했다는 뜻이 아니며 기본 decoded object 여부만 확인한다.
2. `client.notify_initialized()` 한 번. 이는 **notification**이고 RPC request 수에 넣지 않는다.
   반환은 전송 ACK가 아니다. 실패하면 quota call은0이다.
3. `client.call("account/rateLimits/read", {})` 한 번.

RPC call 진입은 호출당 최대2개, notification 진입은 최대1개, retries=0이다. 어떤 단계의
예외에도 이후 호출·자동 재시도·fallback을 하지 않는다. configRequirements/read, thread/resume,
thread/settings/update, queue/add/list/start, usage reset/credit 소비는 호출하지 않는다.
reader가 반환 report를 저장하는 IO도 없다. 저장은 caller 책임이다. live wrapper의 exclusive
attempt marker/one-shot guard도 caller 책임이며 이 API를 다시 호출할 권한을 부여하지 않는다.

### 정확한 report

반환은 중첩 raw result를 포함하지 않는 **plain JSON dict 하나**이며 exact keys는 다음과 같다.

```text
scope = "QUOTA_ONLY_READER"
status = OBSERVED_ALLOWED | OBSERVED_BLOCKED | OBSERVED_UNKNOWN | UNCONFIRMED
phase = PRECHECK | INITIALIZE | INITIALIZED_NOTIFICATION | RATE_LIMITS_READ | SANITIZE | COMPLETE
reason
error_code: allowlisted builtin str | null
requests: list[ {method, call_entered:bool, response_observed:bool} ]  # 0..2, 위 순서만
initialized_call_entered: bool
initialized_notification_completed: bool
retries = 0
ordinaryUsageAllowed: bool | null
ordinary_usage_observed_allowed: bool
windows: list[ {limit_id_sha256:str|null, reached_type_is_null:bool|null,
               primary:window|null, secondary:window|null} ]        # 0..16
transport_pin_verified = false
wire_send_verified = false
full_quota_admission_verified = false
direct_external_io_performed = false
thread_operations = 0
settings_changed = false
queue_changed = false
services_changed = false
remote_mutation_authorized = false
native_acceptance_verified = false
replay_permitted = false
native_ready = false
```

requests의 method는 두 고정 문자열만이다. 실제 RPC ID를 추정해 1/2로 써 넣지 않는다.
call_entered는 Python client 호출 경계를 지났다는 뜻이며 **실제 send/서버 접수를 증명하지 않는다**.
response_observed는 client.call이 반환했다는 사실뿐이다. notification_completed도 메서드 반환
관찰이며 server ACK가 아니다. wire_send_verified는 항상 false다.

정상 완료의 phase는 COMPLETE다. quota가 builtin dict이고 아래 selected snapshot이 유효할 때
ordinaryUsageAllowed **is True**면 status=OBSERVED_ALLOWED,
reason=ORDINARY_ALLOWED_OBSERVED, ordinary_usage_observed_allowed=true다.
**is False**면 OBSERVED_BLOCKED/ORDINARY_BLOCKED_OBSERVED/false,
null·누락·다른 type이면 OBSERVED_UNKNOWN/ORDINARY_USAGE_UNKNOWN/false다.
0/1, truthy 문자열/객체를 bool로 승격하지 않는다.

quota response의 selected snapshot 형식/크기 오류는 phase=SANITIZE,
OBSERVED_UNKNOWN/QUOTA_SNAPSHOT_INVALID, error_code=QUOTA_RESULT_INVALID,
ordinary_usage_observed_allowed=false, windows=[]다. strict bool backend 값이 읽혔다면
ordinaryUsageAllowed에는 그 값만 보존하고 다른 type은 null이다. true였어도 snapshot 오류를
허용 관찰로 승격하지 않는다. 소진율·reset 시각·시간 경과로 ordinary 권한을 유추하지 않는다.

이 allowed는 **ordinary backend boolean 관찰**이다. 모든 적용 bucket의 충분성/신선도,
PAUSE/effective settings/cap/controller admission을 확인한 것이 아니다. percentage가100이어도
ordinary true를 false로 덮어쓰지 않으며 모순/소진 상태의 실제 admission은 별도 caller gate에서
차단해야 한다. full_quota_admission_verified와 모든 dispatch 권한은 계속 false다.

PRECHECK의 interface 부적합은 UNCONFIRMED/CLIENT_INTERFACE_INVALID, error_code도
CLIENT_INTERFACE_INVALID이며 requests=[]다. initialize의 non-dict 반환은
UNCONFIRMED/INITIALIZE_RESULT_INVALID, phase=INITIALIZE이며 다음 호출0이다.
client 호출/notification 예외는 해당 phase에서 UNCONFIRMED/CLIENT_CALL_UNCONFIRMED다.
예외 시 ordinaryUsageAllowed=null, ordinary_usage_observed_allowed=false, windows=[]다.

외부 error_code allowlist는 다음 고정 값만이다.

```text
RPC_REQUEST_TIMEOUT RPC_WIRE_LIMIT RPC_LINE_LIMIT RPC_STDOUT_LIMIT
RPC_CONNECT_FAILED RPC_DEPENDENCY_UNSUPPORTED RPC_UPGRADE_REJECTED
RPC_SEND_FAILED RPC_RECEIVE_FAILED RPC_REMOTE_ERROR RPC_RESPONSE_INVALID
RPC_TEXT_REQUIRED RPC_NONFINITE_JSON RPC_SERVER_REQUEST_FORBIDDEN
RPC_NOTIFICATION_LIMIT RPC_DUPLICATE_RESPONSE RPC_RESPONSE_ID_MISMATCH
RPC_RESULT_INVALID RPC_CLIENT_CLOSED RPC_METHOD_FORBIDDEN RPC_PARAMS_FORBIDDEN
RPC_REQUEST_TOO_LARGE
```

reader 자체 code CLIENT_INTERFACE_INVALID/INITIALIZE_RESULT_INVALID/QUOTA_RESULT_INVALID도
고정 허용한다. 예외의 instance dict에 있는 strict builtin str `code`만 외부 allowlist와
비교한다. 임의 `.code` property를 호출하거나 raw exception의 str/repr/args/class name을
보관하지 않는다. 비문자열·미지 code·안전하게 얻을 수 없는 값은
UNCLASSIFIED_LOCAL_OR_TRANSPORT_ERROR로 축약한다. type 검사가 membership/equality보다 먼저다.

### selected quota snapshot과 privacy

새 reader는 decoded dict의 필요한 필드만 소비한다. 전체 response를 dump/deepcopy하지 않는다.
rateLimits는 builtin dict여야 한다. rateLimitsByLimitId가 builtin dict이면 **1..16 entries**를
상한 확인 후 순회하고, null/누락이면 legacy rateLimits 하나를 사용한다. 다른 type·빈 map·
17개 이상을 앞16개로 잘라 성공시키지 않고 snapshot 오류로 닫는다.

map key는 builtin str/strict UTF-8 1..256 bytes이며 hash만 남긴다. legacy의 유효 limitId도
hash만 남기고 null/누락이면 limit_id_sha256=null이다. native ID를 만들어 채우지 않는다.
bucket은 builtin dict다. primary/secondary는 null/누락 또는 builtin dict로, 적어도 하나의
유효 window가 있어야 한다. window의 exact 출력은 `{usedPercent,windowDurationMins,resetsAt}`다.
usedPercent는 bool 제외 builtin int 0..100, duration은 null/누락 또는 int 1..2^63-1,
reset은 null/누락 또는 int 0..2^63-1만 허용한다. float/NaN/Infinity/거대한 int는 오류다.
reached_type_is_null은 원래 필드가 명시적 null이면 true, 생성 schema의 알려진 reached enum이면
false, 누락이면 null이며 알 수 없는 type/enum은 snapshot 오류다.

accountId/limit ID 원문/limitName/plan/model slug/credits/balance/reset-credit/upsell/raw metadata/
credential/exception/응답 원문은 반환·로그·파일·전역 캐시에 남기지 않는다. 알려지지 않은 부가
필드는 접근하지 않고 버린다. stdout/stderr와 직접 file/clock/environment IO는0이다.

정적 schema 참조는 기존 generated `v2/GetAccountRateLimitsResponse.json`
SHA `cb9655e68130116f634ed9353e45b336492e6069a6628f424d1cd52e54eda0e0`,
`v1/InitializeParams.json` SHA `b55124e7c0f36a1789396daa5578217a92be67f053903696236686e59f6f212e`다.
native 전체 schema validator를 새로 만들거나 transport decoder를 복제하지 않는다.

## B · 별도 DB의 durable pre-submission binding

### 공개 API·입력·재사용

```python
initialize_submission_bindings(binding_connection) -> dict

bind_submission(intent_connection, binding_connection, key, expected_intent_sha256, *,
                submission_params_bytes, expected_submission_sha256) -> dict

read_submission_binding(intent_connection, binding_connection, key,
                        expected_intent_sha256, expected_binding_sha256) -> dict
```

caller가 **서로 다른 두 전용 file SQLite 연결**을 제공한다. key/intent pin은 PR18 계약과 같다.
params는 JSON-RPC envelope가 아닌 원래 queue/add params bytes이며 native로 보내지 않는다.
strict builtin bytes 1..131072, SHA lower64hex, 원문 SHA 일치 후 decode 순서다.
PR19의 `_raw`/`_inputs(...,pages=())`, `_decode(...,"INPUT_INVALID")`, `_identifier`, `_text`와
동일한 pinned parser를 호출해 사용한다. single text UTF-8 1..16384, strict3 keys
threadId/clientUserMessageId/input, omitted 또는 빈 text_elements만 지원한다.
JSON/depth/type/hash 검증을 별도 알고리즘으로 복사하지 않는다. 현재 observe API는 UNKNOWN만
받으므로 PREPARED binding에 호출하지 않는다. 작은 parser adapter만 새 모듈에 둔다.

caller setup은 새 TemporaryDirectory의 각 file DB, isolation_level=None, row_factory=None,
text_factory=str, DELETE journal, synchronous FULL/EXTRA, busy_timeout 0..1000ms다.
제품은 경로/연결을 만들거나 닫거나 PRAGMA를 보정하지 않는다. 두 연결의 구조적 부적합·
caller active transaction을 **양쪽 모두 SQL 전에** 거부한다. 같은 connection 또는
PRAGMA database_list의 같은 main filename은 CONNECTION_INVALID다. 파일 alias/소유자/realpath
인증은 하지 않으며 caller가 서로 다른 전용 DB를 보장한다. attached/temp/memory DB는 거부한다.

기존 public read_intent는 caller transaction이 이미 있으면 거부하고 읽기 뒤 transaction을
닫으므로 두 DB 저장의 PREPARED guard를 원자적으로 유지하는 용도로 감쌀 수 없다.
새 binding 모듈은 pinned PR18 `_connection`, `_schema(...,False)`, `_pinned`와 ledger의
bounded SQL primitives를 **소유한 intent transaction 안에서 재사용**한다. 기존 소스/API는
변경하지 않는다. fixture 전용 controller의 schema/runner를 binding 용도로 바꾸지도 않는다.

### canonical binding과 새 최소 schema

입력 B의 project/batch/task/intent SHA와 검증한 요청 thread/client/params SHA를 다음
**exact object C**로 만든다. canonical은 기존 collector sorted keys/compact separators/
ensure_ascii UTF-8이며 모든 수는 finite strict int다.

```text
schema: 1
project_id: "agent_project"
batch_id: original intent batch
task_id: original intent task
intent_sha256: expected_intent_sha256
intent_revision: 0
client_user_message_id: validated original client ID
thread_id: validated submission threadId
submission_params_sha256: expected_submission_sha256
```

원문 canonical C bytes와 그 SHA-256을 저장한다. max4096 bytes, strict exact keys/types/IDs/
hash와 canonical bytes를 readback 때 다시 확인한다. 원래 params bytes/text/prompt/settings/
계정/경로/원격 응답은 DB에 저장하지 않는다. intent_revision=0은 저장 시점 guard의 불변 표시이며
이후 intent의 현재 revision을 덮어쓰는 컬럼이 아니다. 별도 revision 전이나 binding update API는 없다.

새 DB는 application_id=`0x41494231`(AIB1), user_version=1이며 최소 테이블은 다음 하나다.

```sql
CREATE TABLE submission_bindings (
 project_id TEXT NOT NULL, batch_id TEXT NOT NULL, task_id TEXT NOT NULL,
 binding_bytes BLOB NOT NULL, binding_sha256 TEXT NOT NULL,
 PRIMARY KEY(project_id,batch_id,task_id)
) WITHOUT ROWID
```

initialize만 빈 sqlite_master/zero markers를 확인한 transaction에서 이 schema를 만든다.
기존 정상 binding DB의 재initialize도 DATABASE_NOT_EMPTY다. PR18 intent DB/다른 DB에 schema를
덧붙이지 않는다. schema·markers·예상 밖 object/DDL 불일치는 SCHEMA_INVALID이며 수리/migration0이다.
read는 SQL에서 BLOB/string 크기·type을 bounded 조회한 뒤 실제 hash·canonical C·column key를
검증한다. 저장 bytes/hash 손상은 ROW_INVALID로 거부하며 변경하지 않는다.

### 저장 순서와 PREPARED 경합 방지

1. key/params/hash와 두 연결 profile을 검증한다. 파라미터/byte 오류로 intent나 binding을 쓰지 않는다.
2. **intent 먼저 BEGIN IMMEDIATE**로 소유한다. 기존 schema/pin/row 검증을 재사용하고
   `state == PREPARED and revision == 0`을 하나의 필수 guard로 확인한다.
   이미 UNKNOWN이면 INTENT_NOT_PREPARED이며 기존 binding이 같아도 store/reuse하지 않는다.
3. intent lock을 유지한 채 binding BEGIN IMMEDIATE, exact schema 확인, 같은 key의 row를 읽는다.
   기존 row의 bytes/hash/C 형식이 정상이면 이번 요청에서 만든 expected C와 **원문 bytes가
   완전히 같은 경우만** BINDING_REUSED다. 다른 C는 BINDING_CONFLICT다.
   특히 thread 변경·유효 whitespace/key 순서/빈 text_elements 차이에 따른 params 원문 SHA 변경은
   conflict다. 같은 의미라는 이유로 새 params hash를 덮어쓰지 않는다.
   입력이 자체 형식/hash 오류면 앞 단계의 INPUT_INVALID/INPUT_HASH_MISMATCH가 우선이다.
4. 새 row는 client가 intent의 원래 client와 같고, PR19가 반환한 text UTF-8 SHA가 intent의
   prompt_sha256과 같은 경우만 넣는다. 그렇지 않으면 INTENT_BINDING_MISMATCH다.
   기존 row 재사용도 같은 client/prompt 결합을 확인한다. 다른 유효 요청의 conflict는 기존
   C 비교에서 먼저 거부하므로 원래 row를 교체하는 경로가 없다.
5. 새 row INSERT 후 같은 binding transaction에서 raw C/hash/핀을 재조회해 확인한다.
   binding COMMIT 성공과 실제 transaction 종료를 확인하고, 그 뒤 intent read-only lock을
   **ROLLBACK으로 해제**해 종료를 확인한다. 양쪽 확인 뒤에만 BINDING_STORED/new_binding=true다.
   intent row/schema/markers는 어느 단계에서도 수정하지 않는다.

이 lock 순서는 기존 claim의 BEGIN IMMEDIATE와 실제로 경합하므로 binding COMMIT 동안 intent가
다른 connection에서 UNKNOWN으로 전환되는 것을 막는다. 반대로 lock을 잡기 전에 claim이
완료됐으면 PREPARED guard에서 거부한다. 새 binding API가 claim을 직접 호출하지 않는다.
COMMIT 후 lock 해제와 API 반환 사이에 다른 caller가 claim할 수 있으므로 반환의 intent_state는
**검증 당시 관찰값**이다. 두 DB가 영구적으로 하나의 atomic transaction이라고 주장하지 않는다.

read도 intent→binding 순서로 소유 READ transaction을 사용하고 동일 intent/C pins를 검증한다.
PREPARED0 또는 SUBMISSION_UNKNOWN/revision>=1에서 기존 binding을 읽을 수 있다.
expected_binding_sha256 불일치는 BINDING_PIN_MISMATCH, 다른 intent/client 결합은
INTENT_BINDING_MISMATCH다. UNKNOWN 뒤 C의 intent_revision=0을 현재 revision으로 고치지 않는다.

preflight SQL/BEGIN/INSERT/readback/COMMIT/cleanup 예외는 CHECKPOINT_UNCONFIRMED다.
소유 transaction만 실제 in_transaction을 보고 정리한다. caller transaction을 commit/rollback하지
않는다. binding COMMIT 뒤 응답 유실 또는 intent lock 해제 불명도 전체 성공으로 표시하지 않는다.
binding은 이미 남았을 수 있으므로 new_binding=false/record=None로 끝내고 **readback만** 한다.
자동 bind/initialize/claim/repair/reconnect/retry는 없다. intent lock 중단 시 진행 여부도 추정하지 않는다.

### 정확한 binding 결과

exact top-level keys는 `{report,record}`다. record는 CONFIRMED 저장/재사용/조회에만
exact `{binding_bytes:bytes,binding_sha256:str}`이고 initialize/BLOCKED/UNCERTAIN에는 None이다.
report exact keys는 다음과 같다.

```text
scope = "DURABLE_SUBMISSION_BINDING_ONLY"
status = CONFIRMED | BLOCKED | UNCERTAIN
reason
phase = PRECHECK | INTENT_LOCK | INTENT_VALIDATE | BINDING_TRANSACTION |
        BINDING_VALIDATE | BINDING_WRITE | BINDING_READBACK | BINDING_COMMIT |
        INTENT_RELEASE | COMPLETE
database_commit_confirmed: bool
intent_validated: bool
intent_reason: fixed PR18 blocked reason | null
intent_state: PREPARED | SUBMISSION_UNKNOWN | null
intent_revision: int | null
new_binding: bool
binding_transaction_closed: bool
intent_transaction_closed: bool | null  # initialize만 null
remote_io_performed = false
remote_mutation_authorized = false
input_origin_verified = false
native_acceptance_verified = false
replay_permitted = false
native_ready = false
```

CONFIRMED reasons: BINDINGS_READY, BINDING_STORED, BINDING_REUSED, BINDING_OBSERVED.
BLOCKED reasons: ARGUMENTS_INVALID, CONNECTION_INVALID, DATABASE_NOT_EMPTY, SCHEMA_INVALID,
INPUT_HASH_MISMATCH, INPUT_INVALID, INTENT_BLOCKED, INTENT_NOT_PREPARED,
INTENT_BINDING_MISMATCH, BINDING_NOT_FOUND, BINDING_PIN_MISMATCH, BINDING_CONFLICT, ROW_INVALID.
UNCERTAIN reason: CHECKPOINT_UNCONFIRMED.
database_commit_confirmed은 CONFIRMED만 true, new_binding은 BINDING_STORED만 true다.
성공 phase는 COMPLETE, 실패 phase는 실패가 관찰된 고정 단계다.
기존 PR18 `_Failure`의 알려진 BLOCKED reason은 INTENT_BLOCKED+intent_reason으로 축약하고,
SQL/분류 불명은 UNCERTAIN으로 처리한다. raw 예외/DB path/params는 출력하지 않는다.
양쪽 connection의 preflight 부적합은 앞서 정한 local CONNECTION_INVALID이며,
INTENT_BLOCKED 매핑은 INTENT_VALIDATE의 기존 intent schema/row/pin 검증 실패에 적용한다.
intent_state/revision은 검증됐을 때만, initialize 또는 미검증이면 null이다.
transaction_closed는 실제 borrowed connection 상태이며 init의 intent connection은 존재하지 않는다.

실제fileSQLite commit/readback을 확인해도 native accept·owner origin·transport pin·dispatch 권한은
증명하지 않는다. binding을 가진 caller가 기존 claim API로 UNKNOWN을 commit하고 actual send를
연결하는 단계는 이번에 구현하지 않는다. BINDING_REUSED/OBSERVED도 replay 허가가 아니다.

## 새 검사: 총6 methods, fake client와 실제 SQLite를 구분

새 파일은 `tests/test_codex_unattended_quota_reader.py`와
`tests/test_codex_unattended_submission_binding.py`다. 정확히 아래6개만 최초1회 failfast로
실행한다. 기존 TestCase를 상속하거나 과거 test method를 실행하지 않는다.
새 snapshot/연결 setup/no_external_io 함수는 기존 검증 helper를 함수 단위로 재사용한다.

| method | 필수 입력·관측·완료 기준 |
|---|---|
| A `test_quota_sequence_sanitization_and_fail_closed` | 새로운 fake-owned client만 사용. true/14 weekly와 false/100의 고정 응답, null/잘못된 selected windows·16/17 bucket·int/bool/float/비유한·누락·범위 경계. 정확 initialize→notification→read, requests<=2/notification<=1/retries0, caller context/close0. false/unknown은 allowed flag=false, raw metadata/credit/IDs/exception sentinel과 stdout0, strict report keys/types/code/phase. wire_send_verified/transport_pin_verified/admission flags=false |
| A `test_quota_fault_phases_and_no_send_inference` | 각 단계 직전/호출 중/반환 후 검증의 fake 오류: init 오류 뒤 notify/read0, notify 오류 뒤 read0, quota timeout 뒤 retry0. allowlist code는 보존하고 unknown/hostile code equality는 호출하지 않음. fake call entered 뒤 **실제 send counter0**인 경우 report가 송신/접수 성공을 주장하지 않음. phase·call/response 수와 error code를 확인. 직접 socket/file/clock/credential/env/subprocess/thread/queue/settings/service 접근은 BaseException guard로 실패시키고 reader가 숨기지 못하게 함 |
| A `test_unknown_ordinary_never_allowed` | **M_Q 단일 target.** 나머지는 모두 정상인 14% weekly snapshot에서 ordinaryUsageAllowed=null만 사용. 2 fake calls/notification1 후 OBSERVED_UNKNOWN/ORDINARY_USAGE_UNKNOWN/allowed flag=false. null→false 자동 보정이나 percent/reset 근거의 회복 추론 없음 |
| B `test_binding_prepare_reuse_conflict_and_reopen` | 서로 다른 실제 temp file DB 2개. explicit init→PREPARED0 intent→bind commit→다른 connection 가시성→close/reopen raw canonical C/hash/모든 pins 동일. 동일 원문 reuse/new_binding=false·row1개. thread 또는 whitespace/key order/empty spans 등 유효한 다른 params SHA는 conflict·원래 bytes 보존. 이후 **fixture가 기존 claim API를 한 번 호출**해 UNKNOWN1로 만든 뒤 같은 binding pin read는 성공, bind/reuse는 INTENT_NOT_PREPARED, 상태 재arm0. raw prompt/params/계정·경로 sentinel은 binding DB/공개 report에 없음 |
| B `test_binding_prepared_guard_and_lock_boundary` | **M_B 단일 target은 이 method의 `case_unknown_intent_cannot_create_binding` helper.** 먼저 정상 intent를 fixture에서 UNKNOWN1로 만들고 빈 정상 binding DB에서 신규 bind가 INTENT_NOT_PREPARED/record=None/new_binding=false·row0임을 확인. 같은 method의 별도 정상 subcase에서는 PREPARED0 bind의 binding COMMIT hook 동안 이미 열린 intent observer connection의 BEGIN IMMEDIATE가 실제 lock으로 거부되고 원래 intent가0인 것을 확인. 반환 후 fixture claim만 성공하고 binding은 immutable. 제품의 intent INSERT/UPDATE/claim API 호출0, lock order intent→binding·소유권/conn profile 보존 |
| B `test_binding_faults_pins_schema_and_privacy` | 새 binding schema의 empty/init/reinit/다른 object·marker/동일 연결·같은 main DB·caller active txn을 보존하며 거부. 입력 raw pin/type/PR19 parser 오류, 다른 intent pin/client/prompt·binding pin과 저장 C/hash/schema 손상 거부. 새로운 두-DB 경계에서 preflight SQL/BEGIN 전후/INSERT 전후/readback/binding COMMIT 전후/intent release 실패를 좁은 hooks로 주입. 예외는 UNCERTAIN/new_binding=false/record=None, 소유 txn만 cleanup·자동 재시도0. 실제 COMMIT 뒤 응답 유실은 재open에서 원래 C가 존재하고 조회만 수행. 양쪽 기존 rows/bytes/hash와 unrelated txn·markers/profile 보존·no raw exception/secret |

실제 DB 경계는 tests가 연 전용 TemporaryDirectory의 stdlib SQLite 파일뿐이다. race 검사는
기존 file connection의 bounded SQL lock 경쟁이며 daemon/worker/task 실행이 아니다.
새 모듈은 narrow fault hooks `_execute(connection,sql,parameters=())`, `_commit(connection)`,
`_rollback(connection)`을 제공할 수 있다. 기존 `_one` 주입은 실제 arity `(connection,sql)`를
지키고 각 실패 지점 도달을 확인한다. hook가 틀려 생긴 예외를 제품의 불명 처리 PASS로 세지 않는다.

두 module과 검사·relative fixture를 **첫 실행부터 다른 이름의 최소 source copy**에 복사해
hash 일치를 확인한다. protocol/schema fixture는 상대 경로에 정확한 원문 hash로 보존한다.
private checkout·operator `/tmp` 파일·`/opt` 읽기를 차단하고 새 code가 직접 외부 IO를 시도하면
BaseException으로 실패시킨다. fake client method 호출만 허용하며 테스트 자체도 실제 RPC0이다.
실제 기존 owner-client send/ACK/turn/controller·서버 권한 강제는 이 통과로 인정하지 않는다.

## D38 · 최소 semantic mutants 정확히2개

정상6개 PASS·FAIL0/ERROR0/SKIP0 뒤에만 격리된 source 사본에 적용한다. 정상 target PASS는
최초 결과를 재사용하고 변이별 지정 target 하나만 한 번 실행한다.

1. **M_Q**: A의 ordinary gate **`ordinary is True`를 `ordinary is not False`로** 바꾼다.
   정상 false 분기는 유지하고 null 처리의 실제 잘못된 OBSERVED_ALLOWED/allowed flag=true가
   A `test_unknown_ordinary_never_allowed`의 semantic **FAIL1/ERROR0**으로 드러나야 한다.
   valid window/형식/호출순서/phase/원문 privacy는 그대로여야 하며 다른 guard 오류는 성공이 아니다.
2. **M_B**: B의 **PREPARED and revision0 guard 전체만** 우회한다. 다른 parser/hash/schema/
   insert/readback/commit 검사는 유지한다. C의 불변 intent_revision=0 표시는 builder의 고정
   값으로 유지하여 다른 type/schema guard가 이 의도적 오류를 가리지 않게 한다.
   `case_unknown_intent_cannot_create_binding` helper만 isolated callable target으로 실행한다.
   실제 UNKNOWN1 intent인데 BINDING_STORED/new_binding=true와 **실제 commit된 binding row**가
   잘못 생기는 것을 확인하고, 원래 기대 row0/거부의 semantic **FAIL1/ERROR0**이어야 한다.
   report flag만 틀린 경우나 SQL/import/fixture 오류는 검출 성공이 아니다. intent UNKNOWN1은
   유지되며 실제 queue/send는 없다.

guard 수를 늘려 지정 변이를 가리거나 테스트 기대값을 제품에 맞춰 낮추지 않는다. 두 변이는
quota 허용 판단과 PREPARED 저장 순서라는 새 위험을 검증하며 기존 PR18/19 변이를 반복하지 않는다.

## 실행 순서·근거·완료선

1. 지금 PLAN_COMPLETE/SHA를 남기고 멈춘다. 총괄이 frozen 사본과 reviewer 사전 PASS를 확보한다.
2. 총괄은 두 제품 module, test_agent는 위 새 검사/fixture/runner만 병행 작성한다.
   초기 source/test/fixture/schema/plan/prereview/environment pins를 실행 전에 보존한다.
3. 최초6개를 source copy에서 한 번 failfast 실행한다. FAIL/ERROR/SKIP은 최초 raw log·입력 pin·
   source·관찰/cleanup 상태를 보존하고 중단한다. 같은 실패 fix<=3와 원래01:57:31Z 마감을
   지키며 승인된 원인/해당 단계만 수정·재검증하고 통과 항목/전체 suite를 반복하지 않는다.
4. 모두 통과하면 지정2 mutants만 수행한다. 실제 잘못된 allowed 관찰 또는 durable row와
   semantic FAIL1/ERROR0를 분리 기록한다. 정상 코드/기준/fixtures를 변이에 맞춰 바꾸지 않는다.
5. 최종 독립 reviewer가 고정 code와 실제 근거를 검토한다. 사전 PASS는 최종 PASS가 아니다.

실행 근거는 이 `owner-binding-20261017` 아래 새 `run-1`에 정상/변이/수정 단계별로 별도 기록한다.
기존 최초 receipt/오류는 불변이며 최신 결과로 덮지 않는다. quota는 fake client, binding은
실제 file SQLite/close-reopen이라는 범위를 기록하고 실제 owner native acceptance와 분리한다.
D29/D30/D32/D35/D38을 위 새 경계에 적용한다. 화면·LLM·운영 DB migration/복원·장기 부하는
이번 영향이 없으며 기존 필수 인수를 면제하거나 완료시키지 않는다. 전체 DB 백업은 만들지 않는다.

이번 완료선은 두 새 모듈·새6개·지정2 semantic mutants·보존/portable 근거·최종 독립 PASS다.
**실제 owner client를 통한 새 send, ACK/turn/controller 연결, claim→dispatch 순서 인수와
binding 출처/owner/transport pin, quota snapshot freshness/applicable bucket gate는 다음 단계
미완료다.** 그때도 이번 계획을 owner sixRPC/thread/resume 재실행 권한으로 사용하지 않는다.
운영 원본/DB/Docker/auth/main/GitHub 설정/VMS와 scheduler PAUSE/native D18은 변경하지 않는다.
