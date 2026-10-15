# 고정 fixture controller 설정 로더

기준은 PR #14의 `c91e9e628402fcc89c2daa4301d54c3c321221ed`다.
[기존 controller](github-fixture-controller.md)에 명시 설정을 연결한다.
실제 소유자의 승인값은 제공되지 않았으며 이번 검증은 임시 개발 fixture에 한정한다.

`src.ops.tools.github_fixture_config.load_fixture_controller`는 config bytes와
별도 keyword 입력 `expected_code_version`, `expected_source_sha256`, `expected_uid`를
받는다. 어느 기대값도 설치 파일에서 자동으로 보완하지 않는다. 반환 handle은
`run(expected_assignment_sha256, assignment_bytes)`,
`read(job_id, expected_assignment_sha256)`, `close()`와 context manager를 제공한다.
caller는 handle을 반드시 닫아야 한다. 반복 close는 무해하며 닫힌 handle의 run/read는
`HANDLE_CLOSED`로 거부한다. loader가 연 connection과 FD만 소유한다.
호출자의 예외 처리 중에도 정상 반환한 handle은 명시 close까지 사용할 수 있다.

config는 UTF-8 JSON bytes이며 정확히 아래 다섯 키를 요구한다.

| 키 | 의미 |
|---|---|
| `schema` | bool이 아닌 정수 1 |
| `installation_root` | 현재 이 loader와 기존 부품이 import된 설치 root의 절대 정규 경로 |
| `state_dir` | 현재 실행 UID 소유의 전용 0700 디렉터리 |
| `ledger_db` | state 바로 아래의 기존 0600 ledger DB 절대 경로 |
| `control_db` | state 바로 아래의 별도 기존 0600 control DB 절대 경로 |

설치 root에는 `fixture-controller-installation.json`이 이미 있어야 한다.
marker는 `schema: 1`, `code_version` 두 키이며 version은
`[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` 형태다. 외부 기대 version과 정확히 일치해야 한다.
이 label 대조는 Git commit이나 서명의 인증이 아니다. release label과 기대 hash의
대응 및 입력 출처의 승인은 외부 소유자가 담당한다.

외부 hash mapping은 다음 정확히 일곱 경로의 lowercase SHA-256을 요구한다.

- `src/__init__.py`
- `src/ops/__init__.py`
- `src/ops/tools/github_check_audit.py`
- `src/ops/tools/github_fixture_collector.py`
- `src/ops/tools/github_fixture_ledger.py`
- `src/ops/tools/github_fixture_controller.py`
- `src/ops/tools/github_fixture_config.py`

소스와 marker는 current UID 소유의 single-link regular file이고 group/other 쓰기가
없어야 한다. 소스는 파일별 262,144 bytes, marker는 2,048 bytes, config는 16,384 bytes로
제한한다. 설정/marker의 duplicate·unknown·missing key, 잘못된 type·UTF-8·BOM·nonfinite
값을 거부한다. 기대 UID는 bool 아닌 비음수 정수이며 현재 euid와 같아야 한다.

module의 실제 `__file__`과 spec origin도 해당 설치 파일 경로와 같아야 한다.
`src.ops.tools`는 기준 코드의 namespace package를 유지한다. initializer의 부재,
package path와 spec 검색 경로의 단일 위치를 대조한다. 다른 설치를 동적 import하지 않는다.

경로는 상대 경로·`.`·`..`·중복/끝 slash·NUL·URI 표기를 거부한다. 모든 조상과 대상은
FD를 기준으로 `O_NOFOLLOW`를 사용해 열며 symlink를 거부한다. 조상의 UID는 현재
namespace root FD 소유 UID 또는 기대 UID여야 한다. group/other 쓰기가 있는 조상은
그 root UID 소유의 sticky directory만 허용한다. root UID를 0으로 가정하지 않는다.
최종 설치 디렉터리는 기대 UID 소유이고 group/other 쓰기가 없어야 한다.

DB 두 개는 regular·single-link이고 서로 다른 inode여야 한다. state 밖/하위 디렉터리의
DB, 누락, alias, 잘못된 권한과 기존 `-journal`·`-wal`·`-shm`·`-mj*` sidecar를 거부한다.
SQLite 연결 전에 두 파일 모두의 100-byte header와 rollback format 1/1을 대조한다.
닫힌 WAL DB도 연결 전에 거부하며 journal recovery/checkpoint를 수행하지 않는다.
SQLite URI의 실제 파일명은 인코딩하고 `mode=rw`, autocommit, busy timeout 100ms,
synchronous FULL을 사용한다. 두 기존 schema/application ID/version은 기존 부품의
`initialize=False` 검사로 읽기 transaction 안에서 확인한다. 제품은 mkdir/chmod,
DB 생성·초기화·migration, persistent PRAGMA 변경을 제공하지 않는다.

설치/state/DB FD를 유지하고 연결 전후 및 각 run/read 진입 시 설치 pin과 이름/inode,
권한·header·sidecar·schema를 다시 확인한다. 두 DB의 검증이 끝난 뒤에만 public controller를
호출한다. config 오류는 고정 reason의 `FixtureConfigurationError`다. 오류문과 제품 출력에
경로·원문·SQL·원래 exception을 넣지 않는다.

| 거부 조건 | reason |
|---|---|
| 기대값 누락·형식·UID 오류 | `EXPECTATION_INVALID` |
| config JSON·type·shape·경로 문자열 오류 | `CONFIGURATION_INVALID` |
| 설치 파일·marker·설치 조상 권한 오류 | `INSTALLATION_INVALID` |
| marker version 불일치 | `VERSION_MISMATCH` |
| 설치 source hash 불일치 | `SOURCE_HASH_MISMATCH` |
| runtime 설치/origin/namespace/root inode 불일치 | `INSTALLATION_MISMATCH` |
| 전용 state 검증 실패 | `STATE_DIRECTORY_INVALID` |
| DB 경로·inode·권한·sidecar 검증 실패 | `DATABASE_PATH_INVALID` |
| DB header·schema·application ID/version 검증 실패 | `DATABASE_SCHEMA_INVALID` |
| DB 연결·설정·접근·자원 정리 I/O 오류 | `DATABASE_UNAVAILABLE` |
| 닫힌 handle 사용 | `HANDLE_CLOSED` |

handle의 `report`는 검증 당시의 `FIXTURE_CONFIGURATION_ONLY/CONFIGURATION_VERIFIED`
관찰이다. `owner_approval_verified=false`와 기존 아홉 신뢰/병합 flag=false를 유지한다.
run/read는 기존 controller 응답을 그대로 전달한다. artifact가 없는 준비 실패와 실행/DB
응답 불명을 바꾸지 않으며, 자동 재시도·재수집·artifact 재부착을 하지 않는다.

이 검사는 신뢰한 launcher·이미 로드된 Python process/메모리·pyc·stdlib를 인증하지 않는다.
검증 시점 뒤 같은 UID가 적대적으로 경로/코드를 바꾸는 TOCTOU를 native sandbox로 막는다는
보장도 없다. DB 두 개의 영구 pairing·rollback 방지·검사 발행·서명·최신 binding·독립 역할의
실행 출처는 별도 연결 조건이다. 실제 owner pin/경로 제공과 보호된 설치 인수 전에는
운영 사용 가능이나 required-check/trusted gate/병합 승인을 선언하지 않는다.
