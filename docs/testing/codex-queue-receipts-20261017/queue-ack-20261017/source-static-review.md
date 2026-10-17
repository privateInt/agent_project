# QUEUE-ACK-20261017 source 독립 정적 검토

판정: **PASS (STATIC ONLY)**. 고정 source 393행 전체와 기존 dependency/API 계약을 대조했으며 수정이 필요한 구체적 finding은 없다. 검사·최종 실행·native 인수 PASS는 아니다.

- Source: `ack-development/src/ops/tools/codex_unattended_queue_receipts.py`
- Source SHA256: `24812dc20aa9e20f52ed7c920a301800c00f7568e950f1ed4a9782c1d44495af`
- D29 exact plan SHA256: `027cd5bda32eac67e91246b410d2177afb8a108830bde951ffeab7a0351521f7`
- implementation-plan-review/D06 SHA256: `bdfb9175adb149a5295adbca21a32f336d527ecc6903a2ef241d44139a12fc74`
- 실제 HEAD/base PR20: `aef3af98d1bbc9037f1b75906e0217844bb5bd1d`.

source-ready.json의 여섯 dependency를 실제 파일 hash와 대조했다. binding `1663ab454e0f66a5e7f23382b9cf4c047068a0c86f10d895c4ad19a4d57f2a3c`, journal `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`, parser `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`, quota `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c`, ledger `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`, collector `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`가 고정 기준과 일치한다.

| 범위 | 정적 확인 |
|---|---|
| 76–110, 284–292행 | 세 연결 모두 builtin 구조·caller active 상태와 객체 중복을 ANY SQL 전에 거부한다. BEGIN 전에 기존 journal profile을 적용하고 실제 main filename 중복을 거부한다. 알려진 invalid는 CONNECTION_INVALID, profile SQL 예외는 CHECKPOINT_UNCONFIRMED로 남는다. receipt 명시 초기화만 빈 DB에 AQR1/버전1/exact DDL을 만들며 migration/repair는 없다. |
| 152–183, 367–384행 | builtin key/pin/bytes 및 ACK/pin/code 조합·길이 상한을 확인하고 decode 전에 두 원문 SHA를 검증한다. error code는 type 확인 후 고정 enum membership을 검사한다. 기존 raw/inputs/decode/text를 재사용해 strict UTF-8·중복키·BOM·float/nonfinite·depth·exact shape·identifier·단일 text 제한을 유지한다. ACK hash는 serialized decoded result의 hash다. |
| 218–275행 | public read_submission_binding 이후 public read_intent를 사용한다. source BLOCKED는 고정 source_reason으로, 불명/비정상 반환·예외는 UNCERTAIN으로 구분한다. C/intent raw bytes·hash·key·pins·client/thread/params SHA·prompt SHA를 다시 대조하고 두 source transaction이 실제 닫혔는지 확인한다. source DB를 갱신하거나 claim하지 않는다. |
| 267–270행 | M_STATE guard는 `intent["state"] == "SUBMISSION_UNKNOWN" and intent["revision"] == 1` 한 곳이다. 기존 source reader가 허용하는 PREPARED0/UNKNOWN2는 이 guard에서 거부한다. builder/row validator에 같은 state gate를 중복하지 않는다. |
| 297–312행 | M_CLIENT guard는 `ack_client == intent["client_user_message_id"]` 한 곳이다. ACK text는 submission의 UTF-8 bytes/hash와 같아야 한다. builder는 bound client를 사용하며 ACK client guard를 반복하지 않는다. ACK가 없으면 고정 reason의 UNKNOWN, 있으면 ACK_CANDIDATE projection만 만든다. 두 계획 변이가 다른 guard에 가려지지 않는 구조다. |
| 113–149, 304–341행 | P exact fields/types/enum 관계·canonical bytes/hash·column key를 저장/재조회에서 검증한다. 원문 submission/ACK/prompt/exception은 저장·report·global cache에 넣지 않는다. 동일 P만 reuse하고 다른 유효 P는 conflict다. UPDATE/REPLACE/DELETE 경로가 없어 최초 UNKNOWN을 뒤늦은 ACK로 교체하지 않는다. |
| 313–359행 | source 조회가 끝난 뒤 receipt transaction을 소유한다. INSERT 후 bounded readback 및 COMMIT 실제 종료를 확인한다. BEGIN/INSERT/COMMIT 전후 예외는 UNCERTAIN/record=None/new_receipt=false이고, commit 응답 유실 뒤 남은 row를 삭제·재시도하지 않는다. 소유 receipt transaction만 rollback하며 cleanup 예외는 RECEIPT_CLEANUP이다. caller source transaction이나 borrowed 연결을 닫지 않는다. |
| 186–215, 362–393행 | 세 공개 API와 report/record shape가 계획과 일치한다. init/read의 source closed는 인자가 없어 null이고, record는 invalid None source도 실제 _closed의 bool false다. outcome은 confirmed record에만 있고 initialize/BLOCKED/UNCERTAIN에서는 null이다. 고정 status/reason 및 commit/new_receipt flag를 유지한다. |

모든 remote IO/권한·origin/client identity·transport pin/ID matching·deadline·native acceptance·dispatch/replay/native_ready flag는 false다. client/transport/경로/환경/credential/clock/queue 호출이나 연결 생성·close·자동 init/retry는 없다. report의 source_binding_confirmed는 로컬 binding 조회 확인만 뜻한다. source read 간 또는 receipt commit까지의 3DB atomic freshness, caller 신뢰·기한·ID demux·실제 send/ACK/controller는 외부 경계로 남는다.

기존 D06의 preflight SQL fault는 실제 journal._one 두 인자 또는 SQLite authorizer 경계에서 주입해야 한다. source의 narrow _execute/_commit/_rollback과 기존 public source read를 혼동해 arity 오류를 검출 성공으로 세면 안 된다. 이는 검사 구현의 기존 조건이며 이번 검토에서 test 파일을 조회하거나 변경하지 않았다. 정상 4개·정확히 두 semantic mutant의 실제 hit/잘못된 commit witness·close/reopen·privacy·소유 cleanup 및 Python 3.10 실행 근거는 아직 별도 검증이 필요하다. source-ready의 AST3.10 결과는 기존 작성 기록이며 reviewer가 재실행하지 않았다.

이 보고서만 작성했다. 제품/test/계획 수정·import·검사·SQLite SQL·socket/RPC/native/queue/credential/service·다른 agent 조회/호출은 0이며 기존 PR20 PASS/suite를 반복하지 않았다. 원래 canonical 공유 마감 **2026-10-17T02:50:11.827731Z**, old native D18 HELD·PAUSE·격리·기존 11개 변경/오류/실행 근거와 운영 보호 조건을 유지한다. 실제 검사 후 최종 독립 검토는 별도다.
