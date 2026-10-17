# Owner-binding SOURCE_READY 독립 정적 검토

판정: **REQUEST_CHANGES — B의 오류 결과 계약 F1 수정이 필요하다.** A에서는 구체적인 정적 finding을 발견하지 않았다. 두 모듈의 실행·검사·최종 PASS를 의미하지 않는다.

| 고정 입력 | SHA-256 |
|---|---|
| `codex_unattended_quota_reader.py` 전체156줄 | `699f3a6e8993f28d88d7413ac51197a144b6d8b69a01f321da11a17398a4a23c` |
| `codex_unattended_submission_binding.py` 전체315줄 | `1cc5e361ec8cc5268459973a2843bb3ad9a70f2e7f81417edba7eb7b7b5844bf` |
| frozen D29 계획 | `4ef0459db19aa24a806f1eae5701abc04dca6994220a7df67deff889a2b2d265` |
| D29 PASS·D06 명확화 | `14183f1e79baceed04bb3c656a252d67382ec65209409e005e741e002c289499` |

실제 source 해시는 source-ready.json과 일치한다. 검사 파일·다른 agent 상태는 조회하지 않았으며 새 검사 실행0이라는 준비 기록을 실행 근거로 해석하지 않았다.

**F1 / P2 — B 176–186행, 특히185행: initialize 여부를 None 값으로 판정하여 bind/read의 invalid input report가 잘못된다.**

현재 `_result`는 intent_connection is None이면 intent_transaction_closed를 null로 만든다. 그러나 D29 report는 이 필드를 bool|null로 정하면서 **initialize만 null**을 허용한다. 예를 들어 valid key/pins와 binding 연결을 가진 `read_submission_binding(None, binding_connection, key, intent_pin, binding_pin)`은 `_structural(None)`에서 알려진 CONNECTION_INVALID로 거부되지만, 결과는 intent_transaction_closed=null이다. bind의 동일 invalid connection이나 초기 인자 거부 경로도 같은 영향을 받는다. 이들은 initialize가 아니며 `_sqlite._closed(None)`의 false를 반환해야 한다. 이 호출은 실행하지 않고 코드 흐름만 추적했다.

필요한 수정: `_result`에 mode 또는 명시적 initialize flag를 전달해 **API가 initialize인 경우에만** null을 반환한다. bind/read 및 그 초기 인자 오류에서는 실제 closed 확인 결과를 bool로 반환한다. 정상 initialize의 null, CONNECTION_INVALID 분류, 양쪽 구조 검사 후 SQL 순서, record=None·권한 flag=false는 유지한다. 공개 API/report key·기대 의미를 바꾸는 기준 변경이 아니며 제품이 승인된 계약을 구현하도록 고치는 작업이다. 새 method나 전체 suite 없이 기존 신규 B fault/profile method 안에서 invalid intent None의 SQL0·정확한 reason·bool/null 구분을 확인하면 된다.

그 밖에 확인한 정적 경로:

- **A 호출·strict gate:** 102–134행은 interface 확인 뒤 initialize→notification→rateLimits/read만 호출하고 실패 뒤 후속 호출·재시도가 없다. call_entered와 local response 반환 관찰을 구분하고 wire_send_verified=false다. 136–156행은 strict True/False/unknown을 유지하며 selected snapshot 오류에서는 backend true여도 허용 flag를 켜지 않는다. ordinary is True는 지정 M_Q target과 맞는다.
- **A error/privacy:** 75–85행은 BaseException의 instance-dict descriptor를 사용해 code property/__getattribute__를 우회하고 builtin str allowlist만 반환한다. raw exception/response를 stringify·deepcopy하지 않는다. map/window/ID의 type·개수·범위와 UTF-8 hash 투영이 계획에 맞고 직접 IO·clock·transport·thread/queue/settings 호출은 없다. 실제 fake-client privacy·호출 횟수 검증은 아직 필요하다.
- **B preflight/D06:** 196–204행은 양쪽 구조·caller transaction을 SQL 전에 검사하고, 기존 profile 검증은 BEGIN 전에 수행한다. profile의 알려진 CONNECTION_INVALID와 SQL 불명을 분리한다. 205–222행은 intent BEGIN IMMEDIATE 후에만 기존 _schema/_pinned와 PREPARED/revision0 guard를 사용한다. 기존 public read wrapper나 claim을 호출하지 않는다.
- **B 불변성/readback:** 별도 AIB1 exact schema, bounded row 조회와 canonical C/hash 검증이 있다. 239–261행의 C는 original intent/client와 raw params SHA/thread를 묶으며 기존 row 원문이 다르면 conflict다. 새 row는 client/prompt 결합 확인 후 INSERT·같은 transaction readback을 거친다. read mode는 PREPARED/UNKNOWN의 기존 binding을 조회하고 expected binding pin/intent/client를 확인하며 C.revision을 변경하지 않는다.
- **B commit·소유 cleanup:** 262–285행은 binding COMMIT 및 종료를 확인한 뒤 intent ROLLBACK 해제를 확인한다. commit이 실행된 뒤 예외가 나면 전체 결과는 UNCERTAIN/record=None/new_binding=false가 되고 자동 bind·claim·재연결은 없다. BEGIN 실행 후 응답 유실도 소유 flag와 실제 in_transaction을 보고 정리한다. cleanup 순서는 binding→intent이고 caller active transaction은 preflight에서 거부한다. 실제 lock 경쟁·commit/release 유실·connection/row/file 보존은 정적으로 통과 처리하지 않으며 계획된 실제 temp SQLite 검사 근거가 필요하다.

두 DB의 영구 atomicity·반환 뒤 PREPARED 유지·owner/provenance·native authorization은 확인하지 않는다. 연결/pin/deadline과 후속 claim/send 직렬화는 caller 책임이다. 기존 parser/journal/schema/claim source와 이전 오류·PASS를 수정하거나 재실행하지 않았다.

검사/import/AST 실행/SQLite SQL/socket/RPC/queue/service/credential 접근·다른 agent 조회·위임·통지 없이 source/계획을 읽고 이 보고서만 작성했다. scheduler/root/user PAUSE와 native D18 HELD·기존 조사 상한을 유지한다. 원래 공유 시작 2026-10-17 00:57:31Z, 마감 01:57:31Z이며 시계를 재설정하지 않는다. F1 수정 후 exact source delta 정적 확인과, 새 검사 실행 뒤 별도 최종 독립 검토가 필요하다.
