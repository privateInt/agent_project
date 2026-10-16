# U01b2 · 오프라인 queue/list correlation observer D29 계획

ID: **U01b2-D29-v2**. 상태: **독립 사전 검토 대기 / 구현·검사 미실행**.
이번 변경은 이 계획 파일 하나뿐이다. 제품·검사 import/실행, SQLite 연결/SQL,
native socket/RPC/usage/권한 probe, 다른 agent 조회·위임·통지를 하지 않았다.

## 기준과 독립성

고정 기준은 PR18 **`780d0755378b9f95f0282ec766ca99300b2b0b53`**이다.
`../intent-development`의 실제 HEAD가 이 commit이고 미커밋 변경이 없음을 조회했다.
아래 세 제품 파일은 worktree bytes와 해당 commit의 `git show` bytes가 같다.

| 정적으로 읽은 파일 | SHA-256 |
|---|---|
| `src/ops/tools/codex_unattended_intent_journal.py` | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| `src/ops/tools/github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `src/ops/tools/github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |
| 보존 전체 계획 `agent-unattended-plan-v1.md` | `9bef073e773d2ed10cd329f38a6ff86f28904b54776c418f02ac4c291523c98d` |
| 보존 범위 계획 `agent-unattended-plan.md` | `57c43182b0ba07e1bb4ea0510e3b79b119fe6b761ea70cf58916c99cf8ef0d88` |
| `agent-unattended-intent-plan-v1.md` | `4c005dd7c2a7703f0639ef71aed968c5a1fc5cf8b6ce404364debb779097d575` |
| 위 intent 계획 사전 PASS | `f1fb9dad4822a897f13239cc5fc5fdae4734b7000240a21f056f78767cd476ee` |

**이 제한된 오프라인 부품은 외부 조치 없이 구현할 수 있다.** U01b1의 공개
`read_intent(connection, key, expected_intent_sha256)`와 caller가 제공한 bounded bytes만
사용한다. caller-owned DB의 읽기 transaction을 제외하면 외부 IO가 없는 평가다.
기존 저널 API/schema/상태/report를 바꾸지 않고 새 테이블·migration·저장 상태도 만들지 않는다.

이번에 선택하는 한 가지는 **bounded queue/list snapshot 평가**다. 응답 유실 후 기존 intent와
같은 client/prompt의 관측 후보를 구별하는 데 유용하다. ACK 전용 입력·ACK와 list의 합성,
turn 추적·terminal 완료·durable observation 저장은 후속으로 둔다. `queue/add`를 보내거나
가짜 dispatcher loop를 만드는 작업도 아니다. `queue/add` params는 비교할 caller 입력일 뿐이다.

저널 B에는 thread ID, queue ID, 실제 RPC params hash가 없다. queue/list 응답에도 thread ID가
없다. **이번 API가 이 빠진 영구 결합이나 입력 출처를 인증한 것처럼 표시하지 않는다.**
caller의 제출 params와 각 list 요청·응답을 서로 묶어 관측 후보만 반환한다.
실제 원격 접수, 현재 실행 상태, 원격 exactly-once·idempotence 보장을 추론하지 않는다.

정책 화면 선택은 사용자 보고이며, 부모가 두 최소 확인 turn의 actual auto_review와 동일 managed boundary를 별도로 검증했다. 근거는 policy-confirmation-20261016-1445/result.json이다. 과거 기록의
PARTIAL_PARENT_ONLY를 현재 차단 사유로 되살리거나 policy를 다시 조사하지 않는다.
**기존 runtime D18은 HELD, scheduler/root/user PAUSE는 그대로다.**
새 단위 parent 기록 `correlation-budget.json`의 시작은 `2026-10-16T14:45:08Z`,
전체 마감은 `2026-10-16T15:45:08Z`다. 계획·검토·구현·검증 모두 이 최대60분 안에
포함하며 이 문서 작성/재개로 시계를 다시 시작하지 않는다. 과거 issue 예산과 별도다.

## 실제 읽은 generated schema와 지원 부분집합

기존 `/tmp/agent-unattended-schema-test-agent-4TpbOU`의 생성 원문을 읽었다.
generator/native 실행이나 schema 재생성은 하지 않았다.

| 원문 | SHA-256 |
|---|---|
| `ClientRequest.json` | `f1425525dae2903cb5b658ee412530ff7a8fcaf78887f182c169b299e7f6b09b` |
| `v2/ThreadQueueAddParams.json` | `60f25b7d3e3357c215bef9d2fe7047200f545e030dca9e00527ac0620cd0c88f` |
| `v2/ThreadQueueAddResponse.json` | `c11b9772788427b19c9f9decf0765e2b41cbdead1aa9c3d39f42fdd70b339314` |
| `v2/ThreadQueueListParams.json` | `b4c889089eee8a0f785ff85e0128a4d3f347353870fa848221096a472223773b` |
| `v2/ThreadQueueListResponse.json` | `be4e0bfa6fc776101f96582543605d2eff6186309a08903203512b99c5005926` |

add params의 필수 세 필드는 threadId/clientUserMessageId/input이다. list params는 threadId만
필수이고 cursor/limit는 선택이다. list response는 data만 필수이며 nextCursor는 선택이다.
QueuedSubmission은 id/clientUserMessageId/input을 요구하고 threadId·task/base/settings hash는 없다.
add ACK/list의 QueuedSubmission과 add params/ACK/list의 UserInput 정의가 같은 것도 확인했다.

이 observer는 native 전체 schema validator가 아니다. 승인된 text 작업에 필요한 엄격한
부분집합만 지원한다. 지원 밖 입력은 관측 불명/차단이며 native가 이를 거부한다는 뜻이 아니다.
특히 schema가 nextCursor 생략을 허용해도, **명시적 null 없이 완결성을 추정하지 않는다.**
테스트 작성 단계에서 사용한 세 schema 원문(add params/list params/list response)을 hash 확인해
인접 fixtures에 정확한 bytes로 보존한다. 검사가 `/tmp`나 private checkout에서 schema를
다시 읽거나 generator를 호출하게 하지 않는다. 새 validator 의존성도 필요 없다.

## 정확한 API와 byte/hash 결합

후보 제품은 `src/ops/tools/codex_unattended_queue_observer.py`, 새 검사는 인접
`tests/test_codex_unattended_queue_observer.py`다. 사전 PASS 뒤 PR18 기준 별도 개발 사본에서
만들며 이번에는 이 두 파일/사본을 만들지 않는다. Python 3.10을 유지한다.

```python
observe_queue_correlation(
    connection, key, expected_intent_sha256, *,
    submission_params_bytes, expected_submission_sha256, pages
) -> dict
```

- connection/key/expected_intent_sha256은 U01b1과 같은 borrowed 연결·builtin tuple·pin 계약이다.
  제품이 연결을 만들거나 닫지 않는다. initialize/reserve/claim API를 호출하지 않는다.
- submission_params_bytes는 caller가 의도한 **queue/add params object 자체의 원래 UTF-8 JSON
  bytes**다. JSON-RPC envelope가 아니다. 이 bytes를 보내거나 재전송하지 않는다.
  expected_submission_sha256은 이 원문 전체 SHA-256이다.
- pages는 정확한 builtin tuple이고 원소는 정확한 builtin 4-tuple
  `(list_request_bytes, expected_request_sha256, list_response_bytes, expected_response_sha256)`다.
  list_request는 list params object, list_response는 result object의 원래 JSON bytes다.
  RPC ID/envelope demultiplexing·서버 error 처리·capturing은 caller 책임이다.
  응답 유실은 **마지막 원소의 response/hash를 둘 다 None**으로 표현할 수 있다.
  빈 tuple은 관측 자료 없음이다. generator/callback/임의 iterable을 받지 않는다.
- pin은 모두 strict builtin str/lowercase64hex다. bytes도 strict builtin bytes다.
  type/길이/tuple 개수/전체 byte 합을 확인한 뒤 hash를 비교하고 그 후에만 decode한다.
  equality/encode/iteration보다 builtin type 검사가 먼저다. bytes를 canonicalize해서
  입력 hash 불일치를 숨기지 않는다. whitespace/key 순서가 달라도 해당 원문 pin이 맞으면 허용한다.
- JSON은 strict UTF-8, BOM/duplicate key/NaN/Infinity/비유한 exponent를 거부한다.
  이 부분집합은 float를 전혀 쓰지 않으므로 parse_float는 항상 거부한다. bounded integer·unique
  key helper는 고정 collector의 의미를 재사용한다. recursion/decode/형식 예외는 고정 reason으로
  끝내고 원문 exception/입력을 출력하지 않는다. 지원 object 깊이는 최대8이다.

제출 params의 exact keys는 `{threadId, clientUserMessageId, input}`이다. input은 정확히 하나의
`{"type":"text", "text": ...}`이며 선택 text_elements는 **빈 list만** 허용한다. omitted/[]는
generated default와 같은 빈 span으로 취급한다. 다중 text·비어 있지 않은 span·image/audio/
attachment·추가 keys는 이번 지원 범위 밖이다. 문자열은 strict UTF-8 encode하며 정규화,
공백 trim, 줄바꿈 변환, prompt 재조립은 없다. text UTF-8 길이는 1..16384 bytes다.

제출의 clientUserMessageId는 저널의 원래 client ID와, text UTF-8 bytes의 SHA-256은
B의 prompt_sha256과 같아야 한다. B 원문/hash/project/task/base/plan/settings 등의 검증은
**기존 read_intent**에 맡긴다. 다른 digest의 출처가 확인됐다고 승격하지 않는다.
thread/queue/client IDs는 builtin str, ASCII `[A-Za-z0-9_.:-]{1,128}`만 지원한다.

caller의 expected hashes 자체는 서명/출처 증거가 아니다. 새 입력의 thread ID와 params hash를
저널에 사후 기입하거나 B를 고치지 않는다. 동일 client ID가 원격 idempotency key라는 가정도 없다.

## 순서·pagination 상한과 판정

상한은 observer의 고정 입력 안전 한도이며 owner 설정/실제 native page size 기본값이 아니다.
submission params 최대131072 bytes, 각 list 요청 최대4096 bytes, 각 응답 최대262144 bytes,
전체 submission+모든 요청·응답 합 최대1048576 bytes다. **최대4 pages, page당20 items,
전체80 items**, cursor UTF-8 최대1024 bytes다. 실제 수집·다음 page 요청·재시도·대기는 없다.

1. 외곽 인자·길이·hash를 검증한다. 초과/잘못된 type/pin 형식은 ARGUMENTS_INVALID,
   원문 hash 불일치는 INPUT_HASH_MISMATCH다. 제출 JSON/지원 형태 오류는 INPUT_INVALID다.
   이 단계 거부는 read_intent 호출0이며 어떤 원격/저널 mutation도 없다.
2. 유효 외곽 입력이면 **read_intent를 정확히 한 번** 호출한다. 그 외 직접 SQL은 없다.
   확정 INTENT_OBSERVED가 아니면 평가를 끝낸다. 기존 BLOCKED는 JOURNAL_BLOCKED와 고정
   journal_reason, SQL/commit/cleanup 불명·호출 예외는 JOURNAL_UNCONFIRMED다.
   예외 뒤 재조회/재연결/새 intent/claim은 하지 않는다.
3. 관측된 row가 SUBMISSION_UNKNOWN/revision>=1이어야 한다. PREPARED는 INTENT_NOT_UNKNOWN으로
   차단한다. 제출 client/prompt 결합 불일치는 INTENT_BINDING_MISMATCH다. 어떤 분기에서도
   row/state/revision/원래 bytes/hash를 수정하지 않는다.
4. pages를 순서대로 평가한다. 각 요청은 exact keys `{threadId,cursor,limit}`, 같은 제출
   threadId, **strict int limit=20**이어야 한다. 첫 cursor는 명시적 null, 이후 cursor는 이전
   nextCursor와 정확히 같아야 한다. non-null cursor는 비어 있지 않아야 하며 순환/재사용 불가다.
   원격 response에는 thread가 없으므로 이 결합은 제공된 요청에만 근거한다.
5. 응답은 data list와 선택 nextCursor뿐이다. **모든** item은 exact id/clientUserMessageId/input과
   위 single-text 부분집합을 만족해야 한다. 다른 client item도 지원 불가/잘못된 형태면 안전하게
   건너뛰지 않고 OBSERVATION_INVALID로 끝낸다. 중복 queue ID는 동일 bytes라도
   CORRELATION_AMBIGUOUS다. snapshot이 정적이라는 가정으로 중복을 제거하지 않는다.
6. 응답 None, 빈 pages, nextCursor 생략, non-null 다음 cursor에서 자료가 끝남/4page 상한 도달은
   EVIDENCE_INCOMPLETE다. 잘못된 cursor chain/순환·thread/limit 불일치·malformed/지원 밖 page는
   OBSERVATION_INVALID다. EOF null 뒤 추가 page도 유효 완결 자료가 아니다.
   어느 경우든 부분적으로 찾은 후보는 버리고 candidate=None이다.
7. 모든 page가 검증되고 마지막 nextCursor가 명시적 null인 뒤에만 최종 판정한다.
   원래 client ID item **0개**면 ABSENT_UNRESOLVED, **2개 이상**이면
   CORRELATION_AMBIGUOUS다. **정확히1개**일 때 그 item text의 UTF-8 bytes가 제출 text bytes와
   정확히 같은지 확인한다. 다르면 CONTENT_MISMATCH, 같을 때만 CANDIDATE_OBSERVED다.
   다른 client의 같은 prompt는 후보가 아니다. client만 맞는 잘못된 prompt도 후보가 아니다.

상한·형식 위반은 자동으로 page size/cursor/설정을 고치거나 후속 page 요청을 만들지 않는다.
완결된 빈 목록도 consumed/이미 실행/다른 시점 관측일 수 있으므로 확정 미실행/실패가 아니다.
완결성은 **제공된 page chain**에 대한 속성일 뿐, native의 원자적 snapshot·현재 유일성·신선도
증명이 아니다. 타임스탬프/clock/TTL로 출처나 접수를 추정하지 않는다.

## 정확한 sanitized 결과와 UNKNOWN 보존

반환 exact top-level keys는 `{report, binding, candidate}`다. report exact keys:

```text
scope = "OFFLINE_QUEUE_CORRELATION_ONLY"
status = OBSERVED | UNKNOWN | BLOCKED
reason
journal_read_confirmed: bool
journal_reason: fixed U01b1 reason | null
journal_state: "SUBMISSION_UNKNOWN" | "PREPARED" | null
journal_revision: int | null
pages_examined: int                 # 유효하게 decode한 응답 수, 0..4
items_examined: int                 # 검사한 item 수, 0..80
pagination_complete: bool
input_origin_verified = false
native_acceptance_verified = false
remote_io_performed = false
remote_mutation_authorized = false
replay_permitted = false
native_ready = false
```

- OBSERVED reason은 CANDIDATE_OBSERVED뿐이다.
- BLOCKED reasons: ARGUMENTS_INVALID, INPUT_HASH_MISMATCH, INPUT_INVALID,
  JOURNAL_BLOCKED, INTENT_BINDING_MISMATCH, INTENT_NOT_UNKNOWN.
- UNKNOWN reasons: JOURNAL_UNCONFIRMED, EVIDENCE_INCOMPLETE, OBSERVATION_INVALID,
  CORRELATION_AMBIGUOUS, CONTENT_MISMATCH, ABSENT_UNRESOLVED.
- journal fields는 확정된 read에서만 그 사실을 반환한다. 저널을 읽지 못했으면 state/revision은
  null이다. journal_reason은 고정된 기존 reason만 허용하고 임의 exception 문자열은 내보내지 않는다.
- candidate는 OBSERVED 때만 exact `{thread_id, queued_submission_id, client_user_message_id}`다.
  검증한 요청 thread/원래 client/유일한 matching item ID만 반환하고 나머지는 None이다.
- binding은 모든 원문 pin이 유효하면 exact `{intent_sha256, submission_params_sha256,
  observation_sha256}`다. pin 검증 전 거부는 None이다. observation_sha256은 다음 object를
  sorted keys/compact separators/ensure_ascii/allow_nan=False UTF-8 canonical JSON으로 만든 hash다:

```text
{"schema":1, "intent_sha256":expected_intent_sha256,
 "submission_params_sha256":expected_submission_sha256,
 "pages":[{"request_sha256":req_pin,"response_sha256":response_pin_or_null}, ...]}
```

순서와 응답 유실을 이 manifest에 보존한다. 원문 params/response/input/prompt/result/opaque
cursor, DB 원문/경로, 다른 client/item ID, settings, account/credential, exception은 반환·로그·
파일·DB에 보관하지 않는다. 실제 원문은 평가 중 메모리에서만 소비하며 캐시/전역 보관도 없다.
산출물에는 고정 code·bounded counts·허용 IDs·hash만 남긴다. API stdout/stderr는0이다.

모든 결과에서 저널 UNKNOWN은 그대로다. CANDIDATE_OBSERVED조차 저널을 QUEUED/ACCEPTED/
SUCCESS로 바꾸거나 새 claim/start/T2/replay를 허용하지 않는다. 저널의 원래 revision과
관측 snapshot 간 원자성·동시성·rollback resistance를 이 API가 보장한다는 주장도 하지 않는다.

## D29 검사: 정확히4 methods, 기존 PASS 재실행 없음

명시적인 합성 agent_project/client/thread/queue/prompt만 새 relative fixture에 둔다.
실제 prompt·native response는 fixture나 결과에 복사하지 않는다. 변형한 semantic 입력은
canonical/hash/client/project 결합을 모두 유효하게 만들어 다른 guard가 실패를 가리지 않게 한다.
기존 U01b1 helper의 연결 setup/snapshot/IO 차단을 필요한 함수 단위로 재사용할 수 있지만
그 TestCase를 상속하거나 기존 test method를 실행하지 않는다. public U01b1 API로 setup한
전용 TemporaryDirectory SQLite 파일만 사용한다. setup/cleanup만 연결 생성/종료를 한다.

| method | 반드시 확인할 상황·결과 |
|---|---|
| `test_valid_observation_and_read_only_boundary` | 새 임시 DB에서 reserve/claim한 UNKNOWN1을 사용. 정상1page와 후속 page의 유일 후보, whitespace/key order 원문 pin, omitted/빈 text_elements만 동등 취급. 정확 candidate/binding/hash·flag 전부 false. observer 중 read_intent1회, write API0/direct SQL0/connect/file/clock/socket/process0/stdout0. SQL trace는 기존 read의 조회/BEGIN/COMMIT만, PRAGMA 설정 변경/DML/DDL0. row·raw/hash·revision/파일 bytes/연결 profile 보존, close/reopen 동일. 새 observer의 error mapping으로 원래 pin 불일치→JOURNAL_BLOCKED, 좁은 실제 read hook SQL 오류→JOURNAL_UNCONFIRMED, PREPARED→INTENT_NOT_UNKNOWN만 확인하며 기존 저널 전체 matrix는 반복하지 않음 |
| `test_unknown_and_bounded_evidence` | 완결 empty/다른 client 같은 prompt→ABSENT_UNRESOLVED. 빈 자료/마지막 응답 유실/부분 목록/nextCursor 생략/4pages 후 non-null→EVIDENCE_INCOMPLETE. 첫 page 후보가 있어도 뒤 page 유실이면 candidate=None. 중복ID/다른 queueID의 같은 client→AMBIGUOUS, cursor 순환/chain오류/다른thread/bool limit/EOF 뒤 추가/지원밖 input→관측 불명. 요청/응답/제출 raw hash 변조, duplicate/BOM/float·1e400/과대·잘못된type/hostile equality/잘못된 제출 client/prompt는 지정 차단/불명. 모든 상한 exact boundary와 1초과를 고정 subcase로 검증. 결과에 원문·sentinel·다른 ID·cursor 없음, 수정/재조회/replay0과 UNKNOWN 불변 |
| `test_matching_client_wrong_prompt_remains_unknown` | **단일 M1 target.** 정상 UNKNOWN1, 정당한 제출 params와 전체 hash, 같은 thread의 완결1page·unique queueID·원래 client ID, item의 text bytes만 다른 유효 입력. CONTENT_MISMATCH/UNKNOWN/candidate=None와 raw/hash/revision 보존. hash·schema 오류로 실패시키지 않음 |
| `test_two_matching_queue_ids_remain_ambiguous` | **단일 M2 target.** 정상 UNKNOWN1, 연결된 완결2pages, 서로 다른 queueID 두 개가 같은 원래 client·정확 prompt를 가진다. 정상 cursor/limit·모든 원문 pin 유효, duplicate queueID나 다른 guard 오류 없음. CORRELATION_AMBIGUOUS/UNKNOWN/candidate=None·저널 불변. 첫 후보를 성공으로 반환하지 않음 |

모든 정상/거부 결과에 exact key/type/status/reason·0/1 read 횟수·sanitization을 공통 assertion으로
확인한다. malformed JSON 같은 early byte 거부에는 read0, page 의미 평가에는 read1이다.
실패 주입 시 `_one`의 실제 두 인자 `(connection, sql)`를 유지하고 주입 지점 도달을 확인한다.
제품의 실제 IO 금지 호출은 BaseException guard로 차단해 일반 Exception 처리로 숨기지 못하게 한다.

D30의 실제 범위는 고정 U01b1+실제 stdlib SQLite read/close/reopen뿐이다. generated schema와
native params/response는 합성 protocol fixture 검사다. D32의 불명·중복/역순·pagination과
D35의 다른 thread/client/pin 거부를 위4개에 포함한다. 화면/LLM/운영 migration/부하 검사는
영향이 없고, 실제 native 인수는 미실행이다. input byte·item/page 수의 상한은 필수 경계 검사다.

## D38: 정확히2 useful semantic mutants

최초4개 모두 PASS/FAIL0/ERROR0/SKIP0인 경우에만 source 별도 사본에서 시행한다.
정상 target PASS는 최초 결과를 재사용한다. 각 지정 method 한 개만 변이별 한 번 실행한다.

1. **M1 / `test_matching_client_wrong_prompt_remains_unknown`**: 유일한 matching client item을
   판정할 때 **item text UTF-8 bytes == 제출 text UTF-8 bytes 비교만 생략**한다. 제출 prompt의
   저널 hash 결합과 client/thread/pin/schema/page/유일성 검사는 유지한다. 다른 내용의 queueID가
   CANDIDATE_OBSERVED/OBSERVED로 실제 잘못 반환되는 것을 target의 semantic **FAIL1/ERROR0**으로
   검출해야 한다. 동일 조건의 중복 guard를 넣어 이 변이를 가리지 않는다.
2. **M2 / `test_two_matching_queue_ids_remain_ambiguous`**: 최종 matching-client item 개수의
   **`== 1`을 `>= 1`로** 바꿔 첫 matching item 선택을 잘못 허용한다. 모든 page parse/끝cursor/
   raw hash/서로 다른 queueID/type/내용 결합은 유지한다. 정상 valid 두 후보 중 첫 queueID를
   CANDIDATE_OBSERVED로 잘못 반환해 semantic **FAIL1/ERROR0**이어야 한다. 다른 schema·fixture·
   import·SQL 오류, raw pin 오류 또는 단순 reporter flag 오류는 검출 성공이 아니다.

두 변이도 원격 권한을 부여하거나 실제 요청을 보내지 않는다. 잘못된 **관측 판정**과 실제
반환 candidate를 보존해 검증력을 설명한다. source/test/assertion/fixture를 변이에 맞춰 고치지 않는다.

## 실행·근거·완료선과 live 연결 조건

지금은 계획뿐이다. parent의 exact plan 독립 prereview PASS 후 제품/검사를 작성한다.
새4개를 최초1회 failfast로 실행하고 FAIL/ERROR/SKIP이 하나라도 있으면 최초 근거를 보존하고
중단한다. 전체 suite/기존 R03/R04/R15·transport·U01a·U01b1 PASS를 반복하지 않는다.
변이는 위 gate 이후 지정2개만이다. 이후 correction이 필요하면 원래 결과는 그대로 두고
해당 원인·영향 범위만 별도 승인/재검증하며 원래60분 시계를 유지한다.

후속 실행은 새 `transition-evidence/correlation-run-1`에 source/test/명시 합성 fixture/schema/
plan/prereview/environment hash, 최초 raw log·관측·판정, 변이 source/diff/target 결과를 별도로
보존한다. 실제 native 원문은 담지 않는다. 포터빌리티는 첫4개 실행부터 relative fixture와
별도 clone/source copy에서 검증해 private `/tmp`·`/opt` 의존이 뒤늦게 생기지 않게 한다.
제품/테스트 새 파일, 실제4PASS, 두 의미 변이, 원문/상태 보존, 최종 독립 검토가 이 slice의
완료선이다. 이 D29 문서 자체나 prereview만으로 구현/실행 PASS를 표시하지 않는다.

**외부 선행조건은 live 연결에만 필요하며 오프라인 구현을 막지 않는다.**

| 담당 | 어디서 무엇을 해야 하는가 |
|---|---|
| parent/기존 native runtime 담당자 | 별도 승인된 기존 daemon 연결에서 대상 agent_project 전용 thread와 현재 protocol/schema 호환성, 이미 허용된 읽기 범위를 확인하고 그 범위의 list 요청·응답을 정상 RPC ID와 묶어 수집해야 한다. 이 계획은 endpoint·account·owner 설정을 정하거나 probe/새 daemon/job을 허용하지 않는다 |
| caller/controller 구현 담당자 | 기존 immutable intent/pin·원래 client ID·commit된 UNKNOWN을 그대로 전달하고, 정확한 대상 thread/제출 params bytes·prompt digest와 각 list 요청/응답 bytes를 기록 시점에 hash로 묶는 adapter가 필요하다. 원문은 평가 후 보관하지 않고 sanitized 결과만 남긴다. 이 저널에 없는 thread/params의 사전 영구 결합·출처 보장은 별도 검토된 caller 근거로 제공해야 하며 이번 schema를 몰래 확장하지 않는다 |
| 실제 인수 담당자와 독립 reviewer | 위 출처·대상 결합을 입증한 실제 ACK/queue 관측과 이미 실행·consumed/유실/변동 pagination의 한계를 구분해 인수해야 한다. 이 API의 input_origin_verified/native_acceptance_verified는 계속 false이며 별도 live 근거를 대신하지 않는다. 새 제출/start는 PAUSE·quota·effective settings·cap/순차 조건의 별도 승인/인수 전에는 금지다 |

owner 설정 변경·auth/credential 접근·main/GitHub 보호 설정·운영 원본/DB/Docker·VMS 작업은 없다.
이 계획 작성 전 원본11개 파일 hash가 최초 보존 값과 같은 것을 확인했다. 기존 PASS/최초 오류와
PR18을 수정하지 않았으며 기록된 D18 HELD·PAUSE·조사 상한은 그대로 유지한다.
