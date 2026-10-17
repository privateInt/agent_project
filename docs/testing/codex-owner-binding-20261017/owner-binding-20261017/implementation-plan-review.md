# OWNER-BINDING-20261017 D29 독립 사전 검토

판정: **PASS**. 아래 exact frozen v1과 이 검토의 D06 경미 명확화를 구현·검사 기준으로 사용한다. 제품·검사 실행 또는 최종 독립 검토 PASS는 아니다. 이전 preflight의 PRELIMINARY_ONLY를 승인으로 사용하지 않았다.

- 고정 계획: `review-plan-v1.md`, 31,071 bytes / 412 lines.
- **정확한 plan SHA-256: `4ef0459db19aa24a806f1eae5701abc04dca6994220a7df67deff889a2b2d265`**
- 기준 PR19/실제 binding-development HEAD: `ddec1931eed8828cdd84ae783845729edf617fc1`.
- 기존 intent source pin: `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8`.
- 기존 PR19 parser pin: `0363984b66b46f53776d1991cf0b14d5908ca26c90436616c6f0d737ec96f17a`.
- 실제 오류 분석 pin: `b672de17eba0e4e38d2404dae1b0dbf02b997f679cb0772fe599b752e1f8756c`.
- 보존 quota 관찰 pin: `57a25a332bf29dfe3eabaee2817a1be5d75dd8114177b6a82bac8d51a9e0825c`.

계획 전체와 기존 helper의 연결 검증·schema/pin 조회·transaction cleanup을 정적으로 대조했다. generated GetAccountRateLimitsResponse와 InitializeParams 해시도 계획에 적힌 `cb9655e68130116f634ed9353e45b336492e6069a6628f424d1cd52e54eda0e0` / `b55124e7c0f36a1789396daa5578217a92be67f053903696236686e59f6f212e`와 일치한다. 후속 producer 원본 변경은 이 PASS에 자동 적용되지 않는다.

**A 공개 API/report/error 계약:** read_quota_only(client)는 연결됐고 initialize 가능한 caller-owned client만 빌린다. initialize→initialized notification→account/rateLimits/read의 최대2 RPC/notification1/retries0, 각 실패 뒤 후속 호출0이 명확하다. 실제 transport import·연결 생성/종료·thread/resume·settings·queue·credential IO가 없다. ordinary strict True/False/unknown과 selected snapshot 오류를 구분하고, snapshot 오류에서는 backend true가 있어도 allowed=false로 닫는다. report의 call_entered/response_observed는 local method 경계의 관찰이며 wire_send_verified=false를 유지한다. raw exception/property/response를 복사하지 않는 allowlist code·phase와 bounded 숫자/hash projection이 적절하다.

ordinary true이면서 percent100 등의 snapshot을 실제 admission으로 승격하지 않고 full_quota_admission_verified=false로 남기는 범위도 명확하다. backend boolean 관찰과 모든 bucket/신선도/PAUSE/cap 판정을 분리한 것이므로 기존 fail-closed 실행 기준의 완화가 아니다. 새 fake-client 검사는 실제 owner/native 호출을 대체한 인수가 아니다.

**B 공개 API/report/error 계약:** initialize_submission_bindings / bind_submission / read_submission_binding 세 API와 별도 AIB1 schema, 한 intent key당 한 canonical C, 원문 params SHA 결합이 구체적이다. 같은 bytes만 reuse하며 유효한 다른 thread/params도 conflict다. UNKNOWN에서는 bind/reuse를 거부하고 기존 binding readback만 허용한다. 원래 intent schema/claim/observer를 수정하지 않으며, C의 intent_revision=0은 불변 저장 근거이고 현재 상태를 덮어쓰지 않는다. record 반환 범위와 CONFIRMED/BLOCKED/UNCERTAIN 및 phase/closed flag도 검증 가능하다.

**두 SQLite 잠금 경계:** 공개 read_intent는 active transaction을 거부하고 자체 transaction을 닫으므로 새 저장 guard를 감싸는 데 적합하지 않다. 이 계획은 소스 고정 private validation을 재사용해 intent BEGIN IMMEDIATE→PREPARED0 검증→binding BEGIN IMMEDIATE→binding commit 확인→intent ROLLBACK 해제 순서를 명시했다. intent RESERVED lock은 기존 claim의 BEGIN IMMEDIATE와 경합하므로 binding commit 전에 동시 claim이 확정되는 것을 막는다. intent 데이터 변경 없이 잠금만 사용하며, 결합 commit 뒤 intent release 불명은 전체 성공으로 표시하지 않는다. 이미 남았을 수 있는 binding을 삭제·repair·자동 재bind하지 않고 명시적 readback으로 확인한다. 두 DB의 영구 atomicity·반환 뒤에도 PREPARED 유지·OS crash/rollback resistance는 보장하지 않는다. 후속 claim/send 순서와 caller serialization/pin/owner 인수는 외부 책임으로 남는다.

**D06 경미 명확화 — 아래 두 문구를 기준 해석으로 고정한다.** 기존 단계1–2와 invalid/uncertain 구분을 명확히 하며 기대 의미·검사 강도·범위를 바꾸지 않는다. 사용자 결정이 필요한 중요한 완화는 하지 않았다.

1. “_connection/_schema/_pinned를 소유 transaction 안에서 재사용”이라는 요약은 _connection까지 BEGIN 뒤 호출한다는 뜻으로 구현하면 안 된다. 정확한 문구는 다음과 같다.

   > 양쪽 연결의 builtin 구조·caller active transaction을 SQL 전에 먼저 검사한다. 기존 _connection profile 검사는 BEGIN 전에 호출한다. intent BEGIN IMMEDIATE 뒤에는 _schema(False)와 _pinned를 재사용한다. 기존 _connection은 in_transaction=true를 거부하므로 소유 transaction 안에서 재호출하지 않는다.

2. 기존 PR18 _Failure를 일괄 INTENT_BLOCKED로 바꾸지 않고, 계획의 단계별 분류를 다음과 같이 적용한다.

   | 실패 위치/사실 | 결과 |
   |---|---|
   | 두 연결 preflight의 알려진 구조·profile 부적합 | BLOCKED / CONNECTION_INVALID |
   | 소유 intent 검증의 알려진 schema/pin/row 거부 | BLOCKED / INTENT_BLOCKED + 고정 intent_reason |
   | preflight SQL·BEGIN·INSERT·readback·COMMIT·cleanup 예외/분류 불명 | UNCERTAIN / CHECKPOINT_UNCONFIRMED |

   이 명확화는 알려진 invalid profile과 실행 불명을 혼동하지 않기 위한 것이다. 최초 검사에서 해당 경로와 hook 도달을 실제로 확인하고, 잘못된 helper arity/import 예외를 예상 SQL 불명 검출로 세지 않는다.

**새6개와 정확히2 semantic mutants:** A 세 method는 정상/거부·sanitization·phase/호출 상한·무송신 추론·null ordinary 단일 target을 담당한다. B 세 method는 실제 두 temp file DB의 저장/reuse/conflict/reopen·UNKNOWN 거부/조회·실제 lock 경쟁·commit/release 유실·schema/pin/오류 privacy를 담당한다. 원본 transaction/rows/bytes/hash/profile 보존과 소유 cleanup 범위가 포함돼 있다. 정상6개 최초 PASS/FAIL0/ERROR0/SKIP0 뒤에만 아래 target을 각 한 번 실행하고 정상 target PASS는 재사용한다.

- M_Q: ordinary is True→ordinary is not False. 정상14% window와 ordinary=null에서 실제 잘못된 OBSERVED_ALLOWED/allowed=true가 지정 A target의 semantic FAIL1/ERROR0으로 검출돼야 한다. 다른 type/schema guard로 변이를 가리지 않는다.
- M_B: PREPARED/revision0 guard 전체만 우회한다. 지정 unknown-intent helper만 isolated target으로 실행하며 실제 UNKNOWN1에서 잘못된 BINDING_STORED/new_binding=true와 commit된 binding row가 있어야 semantic FAIL1/ERROR0 검출로 인정한다. C.intent_revision=0 builder와 나머지 hash/schema/commit은 유지한다. report flag 오류·SQL/import/harness ERROR만으로 성공 처리하지 않는다.

첫6개부터 다른 이름의 최소 source copy와 상대 fixture·고정 schema를 사용한다. A의 fake calls와 B의 실제 stdlib file SQLite/close-reopen/lock 경쟁을 구분한다. 기존 R03/R04/R15·U01a/U01b1/PR19 PASS·전체 suite를 반복하지 않는 범위가 적절하다. 실제 send/ACK/controller나 두 native jobs를 통과시킨다는 계획도 아니다.

과거 owner six-RPC/thread-resume writer 충돌과 ordinary=false 기록은 보존하고 최신 true/14% 관찰로 덮지 않는다. 최신 quota 관찰은 PAUSE/native D18 해제 근거가 아니다. 이번 로컬 구현에 별도 native 조치는 필요하지 않지만 실제 owner client·claim→dispatch 연결에는 별도 승인·pin·직렬화·신선도/applicable-bucket 인수가 필요하다.

공유 시계는 **2026-10-17 00:57:31Z→01:57:31Z**이며 계획/검토/구현/검증과 기존 분석 시간을 모두 포함한다. 같은 실패 수정≤3회·최초 실패 보존·해당 단계만 재검증·마감 시 중단 기준을 유지한다. 새 역할이나 파일로 예산을 재설정하지 않는다.

제품/검사 작성·import/검사/SQLite SQL·socket/RPC/network/queue/credential/service 호출이나 다른 agent 조회·위임·통지를 하지 않았다. 이 D29 필수 검토 기록만 작성했다. scheduler/root/user PAUSE·old native D18 HELD·운영 원본/DB/Docker/auth/main/VMS 보호와 기존 조사 상한은 유지한다.
