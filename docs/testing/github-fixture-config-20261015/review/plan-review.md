# L01 독립 사전 검사 계획 검토

판정: **PASS — L01-D29-v1의 아래 고정 SHA에 대한 구현 전 계획 승인**.
검토 ID는 L01-D29-review-v2이다. 실제 구현·검사·운영 설치 인수는 아직 NOT_RUN이며,
이 승인은 최종 코드 검토나 owner 승인·native/trusted gate·병합 승인을 대신하지 않는다.

## 검토 대상과 선행 상태

| 대상 | 고정 값 |
|---|---|
| 기준 commit | `c91e9e628402fcc89c2daa4301d54c3c321221ed` |
| 작업 branch | `codex/github-fixture-config-loader-20261015` |
| 계획 | `review/test-plan.md`, **L01-D29-v1** |
| 계획 SHA-256 | `b350192d28825de5761c448ddff6f73f00dcf778fceec164c2f2134eda353d50` |
| 요구사항 SHA-256 | `02339d17c4f07d2d9a156503cd9f977019f3dbd21418738467b0480061780c75` |
| 공통 정책 | **1.3-preparation** |
| RULES.md SHA-256 | `955e4696c0ac0ac04543fed617fb84010e67b55fce2de9f2f01531c35e7e3352` |
| TESTING.md SHA-256 | `4f37f6ac96ac3d784ceb982daf2c1ee819bf3594e59fde92032caa581cefb037` |
| AGENT_ROSTER.md SHA-256 | `bb71e4d3f1d2b543395b7134c2beb13705be1687c9804b4a2402fe59e0a8c307` |

같은 L01-D29-v1 ID의 계획 내용이 최초 읽기 이후 변경된 것을 발견하여 최종 원문을
다시 읽고 위 SHA에 결합했다. 앞선 계획 SHA `5fce7576…`를 인용한 한국어 작업본은
`review/plan-review-first.md`에 SHA-256
`830b948f114c798271c95f3421f24e96318a34b1320e8ee008622533716ffc63`으로
원문 보존했다. 그 기록을 현재 계획에 대한 승인으로 사용하지 않는다. 최초 작성 중
일본어 본문을 넣었던 문서는 앞선 한국어 작업본 보존 전에 한국어로 정정했으며,
보존 파일을 당시 일본어 원문이라고 표시하지 않는다. 구현 시작 전의 정정이다.

요구사항과 최종 계획 원문, workspace의 AGENTS·진입/개발/검사·프로젝트 규칙 및 기존
audit/collector/ledger/controller 계약과 관련 구현을 읽었다. 작업 workspace의 HEAD가
기준 commit이고 새 loader 구현 전 tracked/untracked 변경이 없는 상태를 조회했다.
제품 import·제품/검사 실행은 하지 않았다. 최초 요구사항의 8파일/UID0 가정은 승인하지
않으며, 위 SHA의 최신 요구사항에 있는 7파일·namespace·root FD 관찰 계약만 승인한다.

읽기 전용으로 확인한 기존 소스 SHA-256은 다음과 같다. 새 loader의 SHA는 구현 후
caller가 제공할 별도 개발 fixture pin이며 지금 만들거나 승인한 값이 아니다.

| 파일 | SHA-256 |
|---|---|
| `src/__init__.py` | `a1f6eb99790b0c12a63bf5ae400c258fe034e6ad7876c4645c2dacdca7145f64` |
| `src/ops/__init__.py` | `39b0de92fc01d71e146b8ceb4719501ea7a985135dc9d8108c3daab9fe652284` |
| `github_check_audit.py` | `d2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e` |
| `github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |
| `github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `github_fixture_controller.py` | `671885e474d4eb2eafe4f786ea8f7075924bbd96250b77ff832561e1b943c5e7` |

마지막 네 파일은 모두 `src/ops/tools/` 아래에 있다.

## 계약·검사 범위 판정

계획의 L01~L20을 요구사항 그룹으로 승인한다. 검사 20개가 실행됐다는 뜻이 아니다.
다음 세부 명세도 구현 전 기준으로 확정한다.

- config exact5키, marker exact2키, 기대 source mapping exact7키를 승인한다.
  config 1..16,384 bytes, marker 1..2,048 bytes, version의 1..128자 ASCII 식별자,
  lowercase64 hex hash, strict type과 bool UID/schema 거부, 고정 11개 오류 reason을
  승인한다. 제품이 caller의 기대 hash를 실제 파일에서 자동 보완하지 않는다.
- namespace는 `src.ops.tools`의 initializer 부재와 `__path__`·spec 검색 위치의 단일
  정규 경로를 확인한다. 정상 namespace의 `__file__`·spec.origin은 None인 Python3.10
  표현으로 취급한다. 존재하지 않는 `tools/__init__.py`를 만들지 않는다. 일반 module과
  package는 실제 `__file__`과 spec.origin을 모두 고정 설치에 결합한다.
- root FD에서 관찰한 UID와 명시 expected_uid의 조상 규칙을 승인한다. 실제 조회에서
  `/`와 `/tmp`는 UID65534, `/tmp`는1777이었다. 이를 UID0으로 꾸미지 않는다.
  최종 state0700, DB0600·regular·single-link, 별도 inode·직속 파일 조건을 유지한다.
  workspace의775/664는 제품이 chmod해 통과시키지 않고 테스트 소유 설치 사본만 준비한다.
- sidecar 거부 목록은 두 DB 각각의 **`-journal`, `-wal`, `-shm`, `DB명-mj*`**로
  확정한다. 0byte·symlink도 SQLite open 전과 각 업무 호출 전에 거부한다. 제품에 기존
  sidecar 삭제·recovery·checkpoint를 추가하지 않는다. L11의 URI 문자 사례는 실제
  basename에 허용되는 `?`, `#`, `%`를 사용한다. `/`는 경로 구분자이므로
  basename 안에 만들었다고 기록하지 않는다.
- 최종 계획에 추가된 **닫힌 WAL DB의 header 사전 거부**를 승인 범위에 포함한다.
  두 DB 모두 SQLite 접속 전에 pinned FD로 최소100bytes, 첫16bytes의 정확한
  `SQLite format 3\0` signature, offset18·19의 write/read format이 각각1인지 확인한다.
  어느 DB의 header라도 잘못됐으면 전체 SQLite connect=0으로 거부해야 한다.
  실제 WAL mode DB를 정상 close해 sidecar가 사라진 fixture도 header format2 때문에
  DATABASE_SCHEMA_INVALID로 거부하고, DB bytes/hash 및 디렉터리 항목을 보존한다.
  load 후 같은 inode에서 header만 바뀐 L17 사례도 각 run/read 업무 API 전에 다시
  거부한다. 제품이 header나 journal mode를 고치거나 WAL/SHM을 생성해서는 안 된다.
- initialize=False의 기존 schema 검증을 두 DB의 읽기 transaction으로 사용할 수 있다.
  읽기 BEGIN/COMMIT와 connection-local 설정 이외의 DDL/DML·persistent PRAGMA,
  공개 controller 업무 API·collector·child는 두 검증이 끝나기 전에 허용하지 않는다.
  빈/0byte/비SQLite/잘못된 또는 WAL header/다른 schema는 DATABASE_SCHEMA_INVALID로 분리하고 일반
  I/O·접속 장애와 임의로 혼합하지 않는다. 제품의 initialize 호출0을 새 검사로 관찰한다.
- L12/L17/L18은 load 후 변경, 실제 temp rename을 통한 검사 전후 inode 차이, 첫·둘째
  connection을 포함한 중간 실패의 자원 정리를 확인한다. owner metadata 주입과 실제
  chmod/link/rename, 실제 연결과 schema·행 보존의 관찰을 분리한다.
- L13~L16을 새 loader 연결 인수로 승인한다. 실제 ok/exit7 child, 원래 artifact의
  bytes/hash/binding 재open, artifact 없는 확정 실패, 합성 SPAWN_FAILED 응답 및
  마지막 read COMMIT 응답 유실 주입을 구분한다. controller 반환을 승격·재부착하거나
  자동 재실행하지 않는다. 마지막 COMMIT 장애는 실제 artifact 기록 이후에 도달했음을
  기록하고 claim 등 앞선 다른 COMMIT 장애로 대체하지 않는다.

## D38 선정: 정확히 두 개

계획의 **M1·M2를 그대로 확정**한다. 추가 변이는 선정하지 않는다.

| ID | 선정한 guard와 주입 오류 | 목표와 필요한 의미 실패 |
|---|---|---|
| M1 | 외부 audit digest와 실제 audit hash의 equality 비교만 생략 | L04의 나머지는 정상이고 audit 기대 digest만 다른 입력. 잘못 handle을 수용해 SOURCE_HASH_MISMATCH 거부 기대 assertion에서 FAIL1 |
| M2 | state exact0700 조건만 생략 | L07의 나머지는 정상이고 state만 실제0755인 입력. 잘못 handle을 수용해 STATE_DIRECTORY_INVALID 거부 기대 assertion에서 FAIL1 |

각 target을 이 단일 음성 사례를 식별할 수 있는 method/subtest에 연결하고 실제 이름을
최초 실행 근거에 고정한다. 특히 M2에 여러 mode를 묶은 target을 주어 여러 FAIL이 생긴
결과를 요구한 FAIL1로 바꿔 기록하지 않는다. 정상 target의 최초 PASS를 재사용하고
각 격리 변이의 해당 target만 한 번 실행한다. 각 FAIL1 / ERROR0 / SKIP0이 기준이다.

변이 loader 자신의 개발 fixture 기대 digest 하나만 해당 변이 bytes에 맞춰 별도 고정하는
계획을 승인한다. 변이 전후 hash와 이 대응 변경을 남긴다. 나머지 6개 source·target test,
의도적으로 잘못된 audit 기대 pin/state0755는 그대로다. 이는 정상 제품이 caller 기대값을
자동 갱신할 권한이 아니다. 자기 hash에서 먼저 거부된 결과나 import/문법/fixture 준비
오류는 목표 guard 검출 성공으로 세지 않는다.

## 공통 기준과 완료선

D28의 AST3.10·새 동작·계약/출력·비밀 비노출, D30의 실제 temp SQLite/기존 부품 연결,
D35의 UID·접근 규칙·DB 분리, D32의 새 연결 경계를 계획대로 적용한다. D31/D36은
화면/LLM 변경이 없어 적용 대상 없음이며, D33/D34에서 새 migration·운영 복원·새 backup을
요구하지 않는 판단을 승인한다. 기존 필수 기준을 해제한 것이 아니다. D37은 nonblocking
FIFO, 입력 상한, bounded busy_timeout, 고정 fixture deadline으로 한정하며 별도 부하·장시간
실행은 요구하지 않는다. 기존 native sandbox D17/D18 소진 보류를 해제하지 않는다.

최초에는 새 module만 실행하고 기존13+3, R03/R04/R15, 전체 suite를 반복하지 않는다.
새 실패는 최초 로그와 source/test/plan pin을 보존하고 D17/D18 안에서 해당 실패 target과
변경 영향에 필요한 범위만 재검증한다. 설정·hash·기대값 변경으로 실패를 숨기지 않는다.
최종 review는 구현 diff와 실제 method/subtest 결과, 두 변이, 자원 정리·보존 근거 및
최초 실패를 각각 대조해야 한다. 필수 미실행/SKIP은 PASS가 아니다. 실제 owner 값·운영
설치·다른 OS 사용자·supervisors/서명/issuer/required-check는 미검증으로 남긴다.

이 reviewer가 작성한 파일은 본 검토 문서와 앞선 작업본의 보존 사본뿐이며,
제품·테스트·계획 원문은 바꾸지 않았다.
제품 구현·테스트 실행·권한 취득·credential/network 접근·추가 위임·MCP 통지를 하지 않았다.
기존 checkout의 11개 변경과 과거 실행 근거 전체 bytes 보존 확인은 coordinator 담당이며,
이 한정된 사전 검토에서 그 전체를 재검증했다고 주장하지 않는다.
