# U01b1 제출 전 intent 원장 구현 가능성

판정: **독립 로컬 구현 가능. 실제 native/owner 계약 부재는 후속 통합의 차단이며 이 단위의 차단은 아니다.**
기존 U01 검토의 journal/CAS 제외는 U01a를 순수 선택기로 좁힌 범위 구분이었다.
전체 submission/transport/recovery를 동시에 구현하지 말라는 판단이며, 이번처럼 작은 SQLite
단위를 별도 D29 계획으로 고정하는 것을 금지하지 않는다. 외부 조치나 새 사용자 승인이
필요한 구현 차단은 발견하지 못했다. 아직 작성되지 않은 exact 계획의 승인으로 확대하지 않는다.

읽은 코드: `var/unattended-20261015/admission-development/src/ops/tools/`의 아래 세 파일과
기존 `/tmp/agent-unattended-plan-review.md`의 U01a/U01b 제외·연결 경계다.

| 파일 | SHA-256 |
|---|---|
| `github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `github_fixture_controller.py` | `671885e474d4eb2eafe4f786ea8f7075924bbd96250b77ff832561e1b943c5e7` |
| `codex_unattended_admission.py` | `b012469262a0de2b9025b393ea2dadc6799933eb1a57fe10c68187809d85da51` |

최소 제약·재사용:

- ledger의 연결 사전 조건·bounded 조회·schema 확인·자신이 시작한 transaction만 정리하는
  `_run` 패턴, pin/bytes 재조회와 `UPDATE … WHERE pin/revision …`·rowcount=1 패턴을 참고한다.
  controller의 기존 claim 재실행 금지와 OUTCOME_UNKNOWN 구분도 재사용한다.
  기존 공개 API/DDL은 fixture assignment/artifact 전용이므로 intent를 거기에 억지로 저장하지 않는다.
- **새 전용 DB와 별도 schema/app ID**만 쓴다. caller가 제공한 connection을 빌리고 모듈은
  path를 받거나 연결을 열고 닫지 않는다. 빈 DB의 명시 초기화와 정확한 기존 journal schema
  확인만 허용하고 기존 DB migration·자동 설정 보정은 하지 않는다. close/reopen은 검사 harness가 맡는다.
- project/repository/root/common-dir·batch/task·base/plan·prompt/settings digest와 client correlation을
  불변 결합으로 고정한다. digest의 bytes/정규화·타입·크기 계약을 사전에 정하고 prompt/비밀 원문은
  기록하지 않는다. 동일 intent 재요청은 기존 행만 반환하고 다른 결합은 충돌로 거부·원본 보존한다.
  같은 논리 작업의 UNKNOWN을 새 correlation/intent로 우회하지 못하도록 identity/uniqueness를 정한다.
- 상태 전이·revision CAS는 한 transaction에서 수행하고 stale CAS·충돌 뒤 원래 bytes/hash/state가
  보존돼야 한다. commit 결과 불명은 확정 실패와 구분하고 같은 ID의 재조회만 허용한다.
  UNKNOWN의 자동 만료·초기화·새 intent 대체·재전송은 없다. 실제 제출하지 않는 단위이므로
  native ACK나 서버 idempotency를 확인한 상태를 만들지 않는다.
- 새 검사 범위는 임시 파일 DB의 저장→close/reopen→정확 재조회, 동일 요청 재사용·다른 결합 충돌,
  stale CAS·UNKNOWN/commit 불명 보존에 한정한다. 기존 PASS·전체 suite는 반복하지 않는다.
  SQL commit/reopen 근거를 전원 장애·스토리지 rollback 저항 증명으로 확대하지 않는다.

미검증 경계: 로컬 DB IO는 발생하지만 queue RPC/transport/worker/settings/PAUSE는 연결하지 않는다.
U01a candidate나 journal 행은 실행 권한이 아니다. 실제 owner/서명/DB 출처·권한·보호된 controller,
서버 correlation·중복 제출 계약, live quota/권한·native 두 작업은 별도 통합 인수다.
D18 held·root/user PAUSE·vms 제외와 운영 원본/DB/Docker/credential 보호를 유지한다.

`/tmp/agent-unattended-intent-plan.md`는 **최초 한 번 조회한 시점에 없었다**.
재조회·polling·wait하지 않았으며 plan-review 파일도 작성하지 않았다. 구현 전 남은 절차는
API/DDL/불변 binding/상태 전이·오류·검사를 고정한 좁은 계획의 exact SHA에 대한 D29 사전 검토다.
이번 작업은 이 검토 파일만 작성했으며 제품·검사 실행/수정, DB/runtime/권한 조회·probe,
기존 결과 수정, 다른 agent polling·위임·통지는 수행하지 않았다.
