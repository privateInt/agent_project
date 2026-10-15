# L01 설정 로더 사전 검사 계획

계획 ID: **L01-D29-v1**. 작성 역할: 별도 test_agent. 상태: 사전 독립 검토 대기.
이 파일 작성 시 제품 구현·검사 코드 작성·검사 실행은 수행하지 않았다.

요구사항은 [requirements.md](requirements.md), 기준 commit은
`c91e9e628402fcc89c2daa4301d54c3c321221ed`, 공통 정책은 1.3-preparation이다.
작성 시 요구사항 SHA-256은
`02339d17c4f07d2d9a156503cd9f977019f3dbd21418738467b0480061780c75`다.
아래 오류 명세는 coordinator의 후속 계약 설명도 반영한 사전 승인 대상이다.
[공통 TESTING](../workspace/docs/codex/common/TESTING.md)의 D28~D38,
[역할 구성](../workspace/docs/codex/common/AGENT_ROSTER.md),
[프로젝트 규칙](../workspace/docs/codex/project.md)을 적용한다.

## 고정 범위와 검사할 경계

새 `src/ops/tools/github_fixture_config.py`와 인접 새 검사만 대상으로 한다.
기존 audit/collector/ledger/controller 및 기존 검사는 수정하거나 실행하지 않는다.
제품 loader는 명시 설정 검증과 기존 connection/API 연결만 수행한다. 동적 import,
exec/eval, CLI, 네트워크, 새 subprocess wrapper, mkdir/chmod, DB 생성·초기화·migration,
자동 재시도·재수집·새 job 생성은 제공하지 않는다.

정상 동작의 기준은 외부 명시 기대값 → 실제 import된 동일 설치의 파일/버전/namespace
대조 → 보호된 state 및 기존 DB 두 개 검증 → 유효 handle → 기존 controller 호출이다.
파일 해시 일치는 외부 기대값의 승인, Git 서명, 메모리/pyc/stdlib/launcher attestation을
뜻하지 않는다. owner 실제 승인값은 NOT_PROVIDED, 운영 설치 인수는 NOT_RUN으로 남긴다.

보존 기준은 [preservation-baseline.json](../preservation-baseline.json)이다.
원래 checkout 변경 11개, 이전 실행과 최초 오류, PR #14 근거13개/수정 후3개 및
R03/R04/R15를 읽기 전용으로 유지한다. 이미 통과한 suite/target은 재실행하지 않는다.

## 사전 확정할 입력·반환 계약

- `load_fixture_controller(config_bytes, *, expected_code_version,
  expected_source_sha256, expected_uid)`의 모든 기대값은 필수 caller 입력이다.
  expected_uid는 bool 아닌 비음수 int이며 현재 euid와 같아야 한다.
- 설정은 정확한 bytes의 UTF-8 JSON object, exact5키
  `schema, installation_root, state_dir, ledger_db, control_db`다. schema는 strict int1.
  marker `fixture-controller-installation.json`은 exact2키 `schema, code_version`이다.
  duplicate/unknown/missing key, nonfinite 값, 잘못된 type/UTF-8/BOM을 거부한다.
  제안 수치: config 1..16,384 bytes, marker 1..2,048 bytes,
  code_version은 1..128자의 비공백 ASCII 식별자 `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}`.
  이 수치와 reason 표는 구현 전에 reviewer가 승인한다. 구현 관찰값으로 소급하지 않는다.
- 외부 hash mapping은 exact7키와 lowercase 64 hex SHA-256 값이다. 경로는 아래와 같다.
  `src/__init__.py`, `src/ops/__init__.py`,
  `src/ops/tools/github_check_audit.py`, `github_fixture_collector.py`,
  `github_fixture_ledger.py`, `github_fixture_controller.py`, `github_fixture_config.py`.
  마지막 다섯 파일은 모두 `src/ops/tools/` 아래다. loader hash는 구현 후 fixture
  설치 시 한 번 계산해 외부 입력으로 고정하고, 불일치 주입 후 다시 계산하지 않는다.
- `src.ops.tools`는 namespace package다. `__init__.py` 부재와 `__path__`의 단일 위치,
  namespace spec의 검색 위치를 검사한다. src/ops initializer는 empty 대역을 만들지 않는다.
- 설치 root/실제 module `__file__`/`__spec__.origin`은 같은 정규 경로의 파일이어야 한다.
  파일 검증을 위해 다른 설치를 동적 import하지 않는다. 이미 로드된 신뢰 process의
  메모리 변경을 검출한다는 주장은 하지 않는다.
- 경로는 절대·정규 경로다. 상대/중복 slash/`.`/`..`/trailing slash/NUL/URI 표기를
  거부한다. root FD의 fstat 소유 UID를 namespace root UID로 사용한다. 조상은 그 UID
  또는 expected_uid 소유이며, group/other writable은 root UID 소유 sticky 조상만 허용한다.
  최종 설치 디렉터리와 소스의 owner/mode도 따로 검사한다. 최종 state는 정확0700,
  DB 두 개는 정확0600·regular·nlink1이며 state 바로 아래 서로 다른 파일이다.
- SQLite는 `uri=True, mode=rw, isolation_level=None`, busy_timeout 0..1000ms,
  synchronous=FULL이다. URI를 조립할 때 실제 path를 인코딩해 옵션 주입을 막는다.
  두 DB의 기존 schema/application_id/user_version 검증은 initialize=False로 수행한다.
  controller의 내부 읽기 전용 schema 검증 helper 사용은 허용하지만, 두 DB 검증 완료
  전 public 업무 API run/read, 초기화, DDL/DML, child 호출은 금지한다.
- 기존 DB `-journal/-wal/-shm`은 0byte·symlink도 open 전에 거부한다. SQLite super-journal
  후보 `DB명-mj*`도 보수적으로 거부하는 것을 제안한다. 최종 sidecar 목록은 사전 reviewer
  확정 대상이며 누락을 구현 결과로 정당화하지 않는다.
- 두 DB 모두 connect **전에** pinned FD로 header를 읽어 최소100bytes,
  exact signature `SQLite format 3\0`, read/write format bytes18/19가 각각1임을 확인한다.
  WAL format2 DB는 정상 close되어 sidecar가 없어도 거부한다. SQLite open이 먼저
  WAL/SHM을 만드는 우회를 막고, header 오류는 DATABASE_SCHEMA_INVALID로 분류한다.
  이 검사는 각 run/read 재검증에도 적용하며 header를 고치거나 journal mode를 변경하지 않는다.
- handle은 run/read/close 및 context manager를 제공한다. close는 idempotent,
  close 후 run/read는 HANDLE_CLOSED다. loader가 연 connection·FD만 소유하고 정리한다.
  load와 각 run/read 진입 시 설치 pin·이름/inode·권한·sidecar를 재검증한다.
  정상 run/read 반환은 기존 controller의 report/record/artifact 의미를 그대로 보존한다.
- handle의 loader report는 scope=FIXTURE_CONFIGURATION_ONLY,
  reason=CONFIGURATION_VERIFIED, fixture_only=true, head_code_executed=false,
  owner_approval_verified=false이며 기존 9개 trust/merge flag가 모두 false다.
  이것과 controller report의 별도 scope/reason을 혼합하거나 성공 상태로 덮어쓰지 않는다.

### 고정 오류 reason 표

각 negative case는 한 조건만 잘못 만들어 reason 우선순위의 모호함을 피한다.
오류는 `FixtureConfigurationError`로 전달하고 고정 reason만 노출한다.

| 조건 | 정확한 reason |
|---|---|
| 외부 기대값 누락/type/format/exact7 mapping 오류, UID != 실제 euid | EXPECTATION_INVALID |
| config bytes/JSON/shape/type/크기/경로 문자열 정규성 오류 | CONFIGURATION_INVALID |
| 설치 marker 누락/shape/owner/mode, 설치 파일 누락·비정규·권한 오류 | INSTALLATION_INVALID |
| 정상 marker의 code_version != 외부 기대 version | VERSION_MISMATCH |
| 정상 외부 mapping의 파일 digest 불일치 | SOURCE_HASH_MISMATCH |
| 검증 root와 실제 module root/origin 불일치, namespace 확장/initializer 존재 | INSTALLATION_MISMATCH |
| state 경로의 owner/mode/symlink/identity/조상 정책 오류 | STATE_DIRECTORY_INVALID |
| DB 경로/owner/mode/type/alias/누락/sidecar/identity 오류 | DATABASE_PATH_INVALID |
| 존재하는 빈/비SQLite/잘못되거나 WAL 형식인 header/다른 schema·application ID·version DB | DATABASE_SCHEMA_INVALID |
| sqlite connect/connection 설정·검증 중 DB 접근 오류 | DATABASE_UNAVAILABLE |
| close된 handle에서 run/read | HANDLE_CLOSED |

설치 경로 조상 정책 위반은 INSTALLATION_INVALID로, state/DB 재검증 실패는 위 해당
domain reason으로 분류한다. 일반 I/O 오류와 schema 거부의 구분도 이 표대로 고정한다.
단순 `except Exception: pass`나 여러 reason 중 아무 값이나 허용하는 assertion은 쓰지 않는다.

## 격리 fixture와 공통 관찰 방법

새 검사 파일 후보는 `src/ops/tools/tests/test_github_fixture_config.py`다.
검사는 이 L01 root 아래 전용 임시 디렉터리/DB만 쓴다. 원래 workspace의 mode664/775를
제품이 완화하도록 만들지 않는다. 실제 6개 pinned 파일과 새 loader를 전용 설치 사본에
복사하고 테스트가 소유한 사본만 directories0755 또는0700, sources0644, state0700,
DB0600으로 맞춘다. 실제 initializer bytes와 namespace 구조도 그대로 복사한다.

새로운 Python child에서 이 설치 사본을 import해 다른 sys.path의 기존 module cache와
섞이지 않게 한다. fresh-process harness는 검사 도구이며 제품 subprocess wrapper가 아니다.
기존 수집기가 고정 child `/usr/bin/python3 -I -B -c`를 실행하는 정상 경로만 사용한다.
테스트가 다른 계정·root·운영 권한을 획득하거나 native sandbox를 조사하지 않는다.

DB schema는 fixture setup에서 기존 명시 초기화 API로 DB별 한 번만 만든 뒤 닫는다.
제품 load/run/read 구간에는 초기화 호출 spy를 0으로 고정한다. SQLite connect는 builtin
Connection을 그대로 반환하는 wrapper로 관찰하며 public controller 업무 API·collector·Popen
호출 횟수도 관찰한다. 가짜 connection을 실제 SQLite 인수로 세지 않는다.

negative 검사에서는 DB bytes/hash, inode, mode, schema/행, 디렉터리 항목을 전후 대조한다.
허용한 connection-local busy_timeout/synchronous 설정과 읽기 transaction의 BEGIN/COMMIT는
domain 쓰기로 합산하지 않는다. persistent PRAGMA/DDL/DML·파일 생성·chmod·child는 0이어야 한다.
테스트가 의도적으로 바꾼 파일은 변경 직후 snapshot을 기준으로 제품의 추가 변경을 검사한다.
atime 변화는 내용 변경으로 오인하지 않는다. SQLite observer용 연결은 비교 단계에서 명시한다.

foreign UID 소유 파일의 실제 생성에 권한이 없으면 fstat/stat owner mismatch의 **명시적
metadata 주입**으로 guard를 검사한다. 실제 chmod·symlink·hardlink·rename 검사는 별도 실제
근거다. 실제 다른 OS 사용자·credential·multi-user/native 격리 인수는 NOT_RUN으로 기록한다.
정상 허용은 실제 euid1011, namespace root/tmp 소유 UID65534 관찰을 숨기지 않고 재기록한다.

## 필수 사례: 입력·행동·정확한 assertion

아래 L01~L20은 요구사항 그룹이며 test method 수나 실행 횟수를 뜻하지 않는다.
새 테스트의 실제 method/subtest 이름·개수는 최초 실행 결과에 별도로 남긴다.

| ID | 입력과 상황 | 필수 assertion / 확인 데이터 |
|---|---|---|
| L01 config 경계 | 정상 exact5키와 bytes 아닌 값, empty/limit+1, 잘못된 UTF-8/BOM, top-level 배열, duplicate/unknown/missing, schema bool/float/2, 비정상 path type·NUL | 정상 fixture만 다음 단계로 진행. 각 잘못된 config는 CONFIGURATION_INVALID, controller/collector/Popen=0, DB 미변경. limit 경계는 JSON 공백 padding으로 입력 자체를 유효하게 유지해 limit와 parse 오류를 구분 |
| L02 외부 기대값 | version/UID/hash mapping 누락·잘못된 type, bool UID, 음수/다른 UID, 누락/초과 key, uppercase/nonhex/63·65자 hash, tools initializer를 요구한 extra key | EXPECTATION_INVALID. filesystem/DB/controller 실행 전 거부. expected_source_sha256를 설치 파일에서 자동 보완하지 않음. 정상 exact7 mapping은 그대로 caller 값과 대조 |
| L03 marker | 누락/빈/oversize/중복/extra key/schema bool/code_version type, symlink/FIFO/권한·owner 오류; 별도 valid marker version 불일치 | 잘못된 marker는 INSTALLATION_INVALID, version만 다르면 VERSION_MISMATCH. FIFO는 blocking 없이 거부. marker를 만들거나 고치지 않음 |
| L04 소스 pin | 7개 파일을 각 subcase에서 한 번씩 외부 정상 형식의 틀린 digest와 비교; 누락/쓰기 허용/다른 owner/비정규 source | digest만 다르면 SOURCE_HASH_MISMATCH, 파일 구조·권한 오류는 INSTALLATION_INVALID. 기대 mapping은 조작 전 고정. 다른 모든 입력이 정상일 때 잘못된 외부 audit digest 하나만으로도 load가 거부됨: M1의 최초 정상 참조 |
| L05 runtime 설치 | A를 import한 상태에서 동일 bytes의 B를 installation_root로 지정; 각 module의 __file__ 또는 spec.origin을 다른 파일로 바꾼 명시적 주입; tools initializer 추가, namespace 검색 위치 두 개 | INSTALLATION_MISMATCH, DB connect/업무 API/child=0. 정상 복사본에서는 root·7 source 경로·package origin·namespace 단일 path 일치. 동적 import/exec로 B를 실행하지 않음 |
| L06 경로와 조상 | relative, `..`, `.`, 중복 slash, trailing slash, file: URI; 설치/state의 final 및 중간 조상 symlink; 소유 fixture 조상의 group/other writable; root-UID sticky /tmp 아래 정상 사본 | 문법은 CONFIGURATION_INVALID, 설치/state walk 문제는 domain reason. nofollow FD walk로 거부; 외부 sentinel·DB 미변경. root-UID sticky 조상만 예외이며 expected_uid 소유 writable-sticky 조상을 root 예외로 잘못 수용하지 않음 |
| L07 전용 state | 0700 정상,0755/0770/1700/2700 등 exact0700 이외, 파일/FIFO/symlink, owner mismatch | 잘못된 state는 STATE_DIRECTORY_INVALID, DB connect/업무 API/child=0. 0755도 반드시 거부하고 handle을 반환하지 않음: M2의 최초 정상 참조. 실제 mode/owner를 기록하고 제품 chmod/mkdir=0 |
| L08 두 DB 경로 | 동일 path, state 밖 또는 nested path, 누락, dir/FIFO/symlink,0644/0660, owner mismatch, hardlink/nlink2 및 같은 inode alias | DATABASE_PATH_INVALID, 누락 DB 생성 없음, 잘못된 파일 connect 이전 거부, 기존 정상 DB와 외부 sentinel bytes 불변. DB path는 이름이 다른지만 검사하지 않음 |
| L09 sidecar | ledger/control 각각의 -journal/-wal/-shm,0byte·symlink 포함; 확정된 super-journal 후보. load 전과 load 후 생성 | DATABASE_PATH_INVALID, 최초 bad sidecar면 SQLite open=0, load 후라면 업무 API/child=0. 기존 DB와 sidecar의 내용/존재 보존, recovery·삭제·checkpoint 없음 |
| L10 실제 header/schema | 두 DB 정상; 각 역할별 빈 SQLite/0byte/비SQLite/truncated header/format18 또는19가1이 아님, application ID/version 변경, 다른 역할 DB, table SQL·추가 객체 불일치. 실제 WAL mode DB를 정상 close해 sidecar가 전혀 없는 경우도 별도 fixture로 준비 | 비정상은 DATABASE_SCHEMA_INVALID. header 오류는 두 DB 중 어느 쪽이든 SQLite connect=0. closed-WAL/no-sidecar 사례는 open 없이 거부해 새로운 WAL/SHM·파일 생성이 없고 DB 원래 bytes/hash 보존. 정상 schema는 기존 initialize=False 검사로 통과; init/DDL/DML/persistent PRAGMA 쓰기=0. 첫 DB 정상·두 번째 schema invalid여도 public 업무 API/child=0, 두 DB 행 보존 |
| L11 실제 연결 정책 | 정상 두 DB와 URI처럼 보이는 문자 `?/#/%`가 있는 **실제 파일명** 사본; connect argument/PRAGMA 관찰 | uri=True이며 인코딩된 실제 경로+mode=rw, isolation_level=None, busy_timeout int0..1000, synchronous=2 확인. filename 문자로 URI 옵션이 주입되지 않음. 두 DB validation 완료 뒤에만 업무 API 전달; load만으로 job row 추가 없음 |
| L12 자원 수명 | 정상 handle close/context exit 두 번; 중간 FD open 또는 첫/두 번째 connect 실패; 검증 실패; handle을 닫은 뒤 run/read | 실패마다 연 builtin connections가 실제 closed, loader-owned FD는 즉시 EBADF, 이후 close할 다른 caller FD는 보존. 반복 close 무해. closed run/read는 HANDLE_CLOSED와 controller/child=0. original exception/SQL/path가 오류문·stdout/stderr에 없음 |
| L13 새 실제 연결 인수 | 고정 ok/exit0와 nonzero/exit7 각각 독립한 설치/state/DB 설정에서 load→run→read→close→다시 load/read | 실제 child 총2개(각 profile 한 번). ARTIFACT_STORED는 저장 관찰이며 exit7 PASS로 바꾸지 않음. 원래 artifact bytes, SHA-256, job·assignment digest, target6+evidence hash의 binding7 모두 일치; 재open read는 child0. ledger revision1과 control CLAIMED를 실제 행으로 확인 |
| L14 확정 준비 실패 연결 | 유효 loader 설정에서 assignment.json만 없거나 확정적으로 잘못됨. run 후 파일을 고쳐 read/run | PREPARATION_FAILED, artifact=None, ledger revision0, control의 원래 preparation_reason 영속. DB를 닫고 새 loader로 재조회해 같음. 수집기 진입은 최초1, Popen=0, 수리 후 재수집·새 job=0. 로더가 assignment를 대신 만들지 않음 |
| L15 불명 연결 | (a) coherent collector SPAWN_FAILED 응답 주입. (b) 실제 수집·저장 뒤 마지막 control read COMMIT의 **실제 commit 후** 응답 오류만 주입 | (a) OUTCOME_UNKNOWN, artifact=None, PREPARATION_FAILED로 기록하지 않음. (b) DATABASE_UNCONFIRMED와 저장 확정 false, 원래 bytes 보유; fault 해제 후 close/reload/read에서 DB bytes/hash 확인. 실제 child는 (b) 한 번. 두 fault는 합성 장애이고 DB/수집 구현 실제 사용 범위를 분리 기록 |
| L16 위임·분리 | 유효 handle run/read의 인수와 반환 identity 관찰; 격리 A/B DB에 같은 job_id와 다른 assignment를 setup API로 미리 저장하고 각각 조회; controller의 비공개 충돌/불명 반환은 boundary stub로 관찰 | 올바른 두 connection/state FD/인수만 기존 API에 전달. 반환 report/record/artifact를 합성·승격·재부착하지 않음. A/B 조회가 각자 DB assignment만 반환, 전역 connection 재사용 없음. stub 근거는 실제 P2/기존 R04 재실행으로 합산하지 않음 |
| L17 load 뒤 변경 | 각 fresh handle에서 run/read 직전 source digest/marker/version/origin/namespace, root/state/DB의 same-bytes 다른 inode 교체, state/DB mode, sidecar, 같은 inode의 header format 변경을 한 개씩 주입 | 원인별 고정 reason으로 **업무 API 전에** 거부하고 child=0. header format만 바꾸면 DATABASE_SCHEMA_INVALID. 기존 held inode와 대체 파일/DB 행을 추가 수정하지 않음. DB byte 비교만으로 same-bytes inode 교체를 수용하지 않음. 각 case는 새 handle로 독립하며 실패한 job을 바꾸지 않음 |
| L18 open 전후 race | 검사의 os.open/SQLite connect wrapper가 사전 경로 검사 뒤, connect 직전 또는 실제 connect 반환 직후 DB/state entry를 동일 bytes 새 inode로 교체 | 고정 DATABASE_PATH_INVALID/STATE_DIRECTORY_INVALID, 연결 전후 pinned inode 대조로 handle/업무 API/child 거부, 이미 열린 자원 모두 정리. 실제 temp rename을 사용한 결정적 race이며 native 동시 적대자 방어 증명으로 표시하지 않음 |
| L19 보고·안전성 | 정상/error/closed 흐름에 PRIVATE_L01_EXCEPTION 등 공개 무해 sentinel을 넣고 stdout/stderr/error text 관찰; 새 source AST3.10/정적 호출 대조 | 고정 reason만 표면화, path/config/raw SQL/original exception/sentinel 비노출. loader report의 fixture/head/owner/9trust flag 정확. 제품에서 추가 subprocess/동적 import/네트워크·init/mkdir/chmod 없음. 기존 collector 내부 Popen은 금지 대상으로 오인하지 않음 |
| L20 보존·전달 | 최종 새 diff/근거와 baseline 대조, code/test/계획/review SHA·실행 환경·명령·첫 실패·부분 재검증 기록 | 기존 checkout11개·기존 module/tests/최초 오류 원문 불변, 허용 새 파일만 diff. 실제 실행과 과거3.12 개발/이전13+3 근거 분리, skip은 PASS 아님. 최종 독립 리뷰 PASS 뒤 별도 브랜치/Draft PR와 고정 원격 commit 파일 bytes 대조는 coordinator 담당 |

L13/L15의 binding7은 artifact를 재직렬화해 대체하지 않고 저장된 원래 bytes의 digest로 만든다.
실제 DB schema 생성은 이 테스트의 새 fixture setup에 한정하며 이전 성공 DB는 초기화하지 않는다.
L17/L18이 다루는 시점 이후의 같은 UID 악성 mutation/메모리 변조/SQLite 자동 recovery 경합의
전면 방어는 범위 밖이다. 검증 시점과 남는 TOCTOU 한계를 최종 계약과 근거에 명시한다.

## D38: 정확 두 개의 격리 변이 후보

reviewer가 아래 두 후보와 target을 확정한 뒤 수행한다. 정상 source의 해당 target은
전체 새 검사 최초 실행에 포함된 PASS를 재사용하고 별도로 다시 실행하지 않는다.
각 변이 설치의 해당 target만 한 번 실행해 **의미상 assertion FAIL1 / ERROR0 / SKIP0**을 요구한다.
문법/import/fixture 준비 오류는 검증력 PASS가 아니며 오류 원문과 단계를 별도로 보존한다.

| 변이 | 의도적 제품 오류 | 목표한 실패 |
|---|---|---|
| M1 | 외부 정상 형식의 audit digest와 실제 audit bytes hash equality guard를 생략 | L04의 bad external audit digest 사례가 handle을 수용해 `expected SOURCE_HASH_MISMATCH rejection` assertion에서 FAIL1. audit expected pin은 정상/변이에서 동일한 잘못된 값이고 나머지는 정상 |
| M2 | 전용 state exact0700 검사만 shared path guard에서 생략 | L07의 실제 state0755 사례가 handle을 수용해 `expected STATE_DIRECTORY_INVALID rejection` assertion에서 FAIL1. 0755는 generic group/other **쓰기 금지** 조건은 만족하므로 다른 guard 오류로 대체하지 않음 |

loader 자신도 hash mapping에 포함되므로, 격리 변이 설치에서는 **변이 loader 파일의 개발용
기대 digest 한 개만** 변이 사본 bytes에 맞춰 고정하고 그 대응 변경을 기록한다. audit pin과
state0755라는 목표 잘못된 입력은 바꾸지 않는다. 이 자기 pin 조정 없이 loader hash 불일치로
먼저 거부되는 결과를 mutation 성공으로 세지 않는다. 다른 6개 source·target test bytes는
불변이며 loader import/origin도 해당 격리 설치를 가리켜야 한다. 변이 내용/전후 hash/선택
target/실패 assertion/normal 최초 근거를 기록하고 배포 산출물에 변이를 포함하지 않는다.

## 실행·재검증·완료 근거

사전 reviewer가 이 계획 **버전 및 SHA**와 위 오류 표/수치/변이를 승인한 후 구현한다.
추가 계약 변경은 이유·영향·검사 delta를 독립 reviewer가 먼저 판단하고 D06/D07을 유지한다.
특히 marker 보호 정책, sidecar 목록, namespace origin 표현은 소스 관찰에 맞춰 몰래 완화하지 않는다.

승인 후 최초 실행 후보는 해당 fresh-install harness를 포함한 **새 검사 module만**의
`python3 -B -m unittest -v src.ops.tools.tests.test_github_fixture_config`다. 작업 디렉터리는
이 작업 workspace이고 실제 Python/SQLite/UID와 fresh installation root를 로그에 기록한다.
새 전체 검사 최초 실행 외에 이미 통과한 검사 반복은 하지 않는다. 실패하면 소스/환경과
첫 로그를 보존하고 해당 실패 target만 `module.Class.test_method`로 지정해 수정·재검증한다.
동일 실패의 분석→수정→관련 재검증은 D17 최대3회, D18 누적60분 중 먼저 도달하는 상한을
적용한다. 과거 native sandbox 문제의 소진 횟수/시간을 초기화하거나 재조사하지 않는다.

근거에는 source/test/계획/승인 review SHA, 명령·cwd·환경, method/subtest 수, FAIL/ERROR/SKIP,
실제 child 수, DB reopen bytes/hash/binding, 주입 장애·metadata 주입·실제 filesystem 사례,
자원 정리와 보존 결과를 분리한다. 실제 적대적 다중 UID와 운영 설치·owner 값·issuer/서명/
required-check 연결은 미검증 목록에 남긴다. 필수 case 미실행/SKIP이면 L01 인수 완료로
표시하지 않으며, 사전 계획 승인과 최종 코드/실행 근거의 독립 승인도 구분한다.

D30은 실제 temp SQLite 두 개와 고정 collector/controller 연결에 적용한다. D35는 명시 기대
UID·filesystem 접근 정책·설치 분리·DB 라우팅에 적용하고 실제 OS 다중 사용자 인수와 구분한다.
D31/D36은 화면/LLM 변경이 없어 적용 대상 없음이다. D32의 기존 claim·실패·불명 의미는 위 새
연결 사례와 위임 검사만 확인하고 기존 suite를 반복하지 않는다. D33~34는 schema 변경·복원/
운영 백업을 추가하지 않아 새로운 migration/운영 복원 검사 대상 없음이며 기존 필수 기준을
해제하지 않는다. D37은 FIFO nonblocking·bounded JSON/busy timeout·고정 fixture deadline만
검사하고 별도 부하·장시간 검사는 요구/실행 경로 변경이 없어 선정하지 않는 안으로 제출한다.

별도 test_agent는 이 파일의 작성자이며 제품 구현자는 아니다. 현재 모든 검사 결과는
**NOT_RUN**이다. 이 계획의 사전 승인과 제품 구현/검사 수행은 다음 담당 단계다.
