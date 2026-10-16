# U01b1 intent journal D29 독립 사전 검토

판정: **PASS — U01b1-D29-v1의 아래 exact pin에 한정**.
구체적인 수정 요구는 없다. 제품 구현·검사 실행 전의 계획 승인이며 실행 PASS가 아니다.

| 대상 | SHA-256 |
|---|---|
| 원 repo `var/unattended-20261015/transition-evidence/agent-unattended-intent-plan-v1.md` | `4c005dd7c2a7703f0639ef71aed968c5a1fc5cf8b6ce404364debb779097d575` |
| 선행 `/tmp/agent-unattended-intent-viability-review.md` | `9fc39eb55d1d050ae500ab40f0a07b6b689374736f9bb124b5327d193070cfd9` |

보존 계획 222행 전체를 읽고 기존 ledger/controller의 transaction·schema 패턴 및 앞선
U01 범위 판정과 대조했다. 기존 API는 fixture schema에 결합돼 있으므로 새 전용 모듈에서
그 transaction/CAS 패턴을 좁게 재사용하는 판단이 타당하다. 기존 모듈·DB migration이나
공용 framework 추출은 포함하지 않는다. 실제 native/owner 계약의 부재는 후속 통합 조건이며
이번 임시 DB 구현을 막는 외부 의존성은 아니다.

승인한 계약:

- 네 API는 initialize/reserve/read/claim뿐이다. caller-owned connection을 빌리고 path·연결
  생성/종료·clock·remote callback을 제공하지 않는다. 연결 profile·기존 caller transaction·
  attached/temp/memory DB를 검사하며 caller transaction이나 PRAGMA 설정을 보정하지 않는다.
- initialize는 빈 object/zero marker의 전용 파일 DB에만 명시적으로 schema를 만든다.
  정상 journal의 재initialize도 거부하며 나머지 API에는 초기화·수리·migration이 없다.
  schema/app ID/version과 허용 table/index가 고정됐다.
- canonical B의 전체 원문 SHA와 project 부분의 canonical hash를 검증한다. 다른 digest는
  trusted caller가 원문과의 일치를 책임진다는 경계가 명시됐다. project/batch/task key,
  base/plan/prompt/settings/task digest와 correlation은 불변이다. 같은 bytes 재요청은 기존
  row만 재사용하며 다른 결합·다른 key의 correlation 재사용은 충돌이다. UNKNOWN을 새
  correlation으로 같은 key에 덮어쓰는 경로도 없다. raw prompt/settings는 저장하지 않는다.
- 정상 전이는 PREPARED/0→SUBMISSION_UNKNOWN/1 한 번뿐이다. key/pin/expected revision/
  state=PREPARED를 **실제 UPDATE predicate**로 강제하고 rowcount와 commit 종료를 확인한다.
  0-row 이유는 같은 transaction에서 구분한다. UNKNOWN의 양의 revision 표현은 정상 재arm을
  허용하지 않으며, M2가 schema 오류에 가려지지 않게 하는 명시적 범위다.
- 고정 error/report와 new_claim은 DB 관측만 설명한다. commit/cleanup 불명은
  CHECKPOINT_UNCONFIRMED·new_claim=false·record=None이다. 자신의 transaction만 정리하고
  자동 retry/reconnect/repair는 없다. 이후 observe만 한다는 caller 의무와 DB rollback 후
  PREPARED/row 없음이 원격 미실행의 증거가 아니라는 경계도 분명하다.

검사 계획은 **새 SQLite 5 methods와 isolated mutants 정확히 2개**로 승인한다.
전용 실제 파일 DB의 다른 connection 가시성·close/reopen·원문/ID/hash 보존, 충돌·stale CAS,
UNKNOWN 재claim 거부, competing CAS, 실제 COMMIT 후 응답 유실과 borrowed 연결 소유권을
다룬다. 손상·schema 오류·공개 비노출과 caller transaction 보존도 포함된다.

M1은 revision predicate/대응 parameter만 생략해 expected1이 rev0을 잘못 claim하는 단일 target,
M2는 state predicate만 생략해 UNKNOWN/rev1을 rev2로 재arm하는 단일 target이다.
각 정상 최초 PASS를 재사용하고, 실제 잘못된 claim의 semantic FAIL1/ERROR0만 검출 성공으로
센다. 다른 guard/SQL parameter/schema/fixture ERROR로 막힌 결과는 성공으로 세지 않는다.
정상 최초 5개에 실패·오류·skip이 있으면 보존 후 중단하며 변이·기존 PASS 반복으로 진행하지 않는다.

이 PASS는 계획에 지정된 별도 개발 사본의 구현·새 검사·독립 최종 검토 절차에 한정된다.
새 DB에는 로컬 SQL IO가 발생하지만 remote_io_performed/remote_mutation_authorized는 false다.
claim 성공도 PAUSE/quota/실행 권한 또는 서버 idempotency·원격 exactly-once 보장이 아니다.
서명·실제 owner/DB 출처·보호된 controller/journal 연결·RPC/ACK·전원 장애 인수·native 두
작업은 이번 완료선 밖이며 NOT_RUN이다. D18 HELD·root/user PAUSE·vms 제외와 기존 운영
보호·원본11개 변경·최초 오류/실행 근거를 그대로 유지한다.

reviewer는 고정 문서·기존 소스만 읽고 이 검토 파일만 작성했다. 제품·검사·계획 수정,
import/검사 실행·DB/SQL/runtime 조회·권한 probe·socket/credential 접근·다른 agent
polling/wait·위임·통지는 수행하지 않았다.
