# L01: 고정 fixture controller 설정 로더

사용자 요청: PR #14 고정 commit에서 외부 기대 코드 버전/해시와 설치 파일을
대조하고 전용 상태 디렉터리·기존 ledger/control DB 경로를 검증해 기존
controller에 연결한다. 누락·불일치는 거부하며 소유자 승인값을 만들지 않는다.
개발 검증은 명시적인 임시 fixture 설정만 사용한다. 기존 DB 자동 초기화와
migration을 추가하지 않는다. 독립 사전 검토 → 구현/새 검사 → 독립 최종 검토
→ 별도 브랜치 commit/Draft PR 및 게시본 대조까지 승인됐다.

기준: c91e9e628402fcc89c2daa4301d54c3c321221ed, 공통 정책 1.3-preparation.
작업 공간: 이 파일의 ../workspace. 원래 checkout의 변경 11개와 이전 실행/최초
실패 이력은 ../preservation-baseline.json과 대조한다. PR #14의 기존 검사13개,
수정 후3개, R03/R04/R15 및 전체 suite는 재실행하지 않는다.

담당: coordinator는 제품 구현·통합/게시, 별도 test_agent는 검사 계획과 새 검사,
별도 reviewer는 사전 계획 및 최종 코드/근거 검토만 맡는다. reviewer는 구현이나
검사를 고치지 않고 MCP 통지를 보내지 않는다. 각 담당은 직접 파일을 남기고
자신의 final 응답으로 완료를 전달한다. 운영/credential/worker/scheduler/main과
기존 sandbox D17/D18 소진 보류는 건드리지 않는다.

## 제안 계약 (사전 reviewer가 검토해 확정)

- 새 모듈 src/ops/tools/github_fixture_config.py, 새 인접 검사, 계약 문서 및
  작업별 소형 근거만 추가한다. 기존 audit/collector/ledger/controller 소스와
  기존 검사는 byte 보존한다. 공유 상태 문서는 필요하면 새 브랜치에서만 작은
  참조 행을 추가하되 원래 checkout을 수정하지 않는다.
- 공개 API load_fixture_controller(config_bytes, *, expected_code_version,
  expected_source_sha256, expected_uid). 모든 기대값은 명시적 caller 입력이며
  실제 소유자 승인·출처 인증을 뜻하지 않는다. fixture 개발 pin을 명확히 구분한다.
- config_bytes는 크기 제한, duplicate/unknown/missing key와 잘못된 type을 거부하는
  JSON이다. 필드는 schema=1, installation_root, state_dir, ledger_db, control_db.
  version은 설치 root의 fixture-controller-installation.json에 schema=1/code_version
  두 키로 기록돼 있으며 외부 expected_code_version과 정확히 대조한다.
- 기대 source mapping은 audit, collector, ledger, controller, 새 loader와 import되는
  src/__init__.py, src/ops/__init__.py의 정확한 7개
  상대 경로와 SHA-256을 요구한다. src/ops/tools는 기준 commit에서 namespace
  package이므로 __init__.py의 부재와 단일 __path__도 대조한다.
  누락/초과/잘못된 hash 거부. 버전 label만의
  비교는 Git commit/서명의 인증이 아니다. 외부 release→hash 대응의 승인은 owner 책임이다.
- 설치 root는 현재 이 loader와 기존 controller bundle이 import된 동일 설치 경로와
  일치해야 한다. 각 runtime module __file__/origin도 설치 root 안의 정확한 파일과
  대조한다. 다른 sys.path 설치를 검증하고 기존 설치를 실행하는 우회를 거부한다.
  동적 코드 import/exec, CLI, 새 subprocess wrapper, 네트워크를 추가하지 않는다.
  이미 신뢰한 Python process/stdlib/launcher의 메모리·pyc attestation은 범위 밖이며
  그 한계를 분명히 한다. 이것을 native/trusted gate 통과로 표시하지 않는다.
- 절대 정규 경로만 허용한다. 각 조상/대상 symlink를 O_NOFOLLOW+dir_fd walk로
  거부한다. 조상의 owner는 현재 namespace의 root FD 소유 UID 또는 expected_uid이며
  group/other writable은 그 root UID의 sticky 디렉터리(예: /tmp)만 허용한다.
  이 서버 root와 /tmp는 namespace mapping상 UID65534로 관찰됐으며 0으로 꾸미지 않는다.
  expected_uid는 실제 euid와
  같아야 한다. 최종 설치 디렉터리와 소스는 expected_uid 소유, group/other 쓰기
  금지; 전용 state는 0700, 기존 두 DB는 0600·regular·single-link를 요구한다.
- DB는 state_dir 바로 아래 서로 다른 파일이어야 한다. inode alias, 외부/동일
  경로, 누락, 빈 DB, symlink/hardlink, SQLite URI 주입을 거부한다. ledger/control
  schema와 application ID/version을 기존 API의 initialize=False 검사로 대조한다.
  두 DB의 validation을 끝내기 전에 controller 호출/쓰기/child 실행은 없다.
- DB connect는 uri=True, mode=rw, isolation_level=None, bounded busy_timeout,
  synchronous=FULL. missing DB를 만들거나 mkdir/chmod/migrate/initialize하지 않는다.
  hot journal/WAL/SHM 등 기존 sidecar는 SQLite open 전에 거부해 자동 recovery 쓰기도
  피한다. 연결/상태 FD는 loader가 소유하며 실패/close 때 정리한다.
- root/state/DB FD를 유지하고 각 run/read 전에 이름→inode, owner/mode/sidecar와
  설치 pin을 다시 대조해 load 후 교체를 거부한다. SQLite 연결 전후에도 pinned inode를
  대조한다. 같은 UID의 적대적 concurrent mutation을 native sandbox로 방어한다고
  주장하지 않는다. 검사 중 race를 주입해 실행 전 fail-closed 경계를 확인한다.
- 반환 handle은 run(expected_assignment_sha256, assignment_bytes),
  read(job_id, expected_assignment_sha256), close/context manager를 제공한다.
  유효 handle만 기존 controller를 호출한다. artifact bytes/hash/binding와 controller
  확정 실패/불명 상태/비공개 충돌 의미를 바꾸지 않는다. 오류는 고정 reason의
  FixtureConfigurationError로 반환하고 경로·원문·SQL/exception/secret를 출력하지 않는다.
- loader report는 fixture_only=true, head_code_executed=false, owner_approval_verified=false,
  기존 신뢰/병합 flag=false다. 실제 owner 값은 이번 작업에서 NOT_PROVIDED/NOT_RUN이다.

## 검사 계획 요구

test_agent가 D29 계획을 먼저 작성하고 reviewer가 버전/해시로 승인한 뒤 구현한다.
새 로더의 정상/거부/실제 임시 SQLite 연결·자원 해제·변경 후 거부만 새로 검사한다.
기존 controller unit tests를 재실행하지 않는다. 실제 고정 child 정상/exit7와 준비
실패 및 불명 결과를 loader 경로로 확인하는 것은 새 연결 인수이며 이전 controller
suite 재실행으로 합산하지 않는다. 임시 DB schema 생성은 test fixture setup에서만
기존 명시 초기화 API로 한 번 수행하고 제품 loader가 init하지 않았음을 계수한다.

D38은 외부 pin guard와 state/DB 경로 guard 각 하나를 후보로 정확히2개 선정해
원본 최초 PASS와 격리 변이의 의미상 assertion FAIL/ERROR0을 대조한다. reviewer가
대상을 확정한다. 잘못된 mutation 주입/메타데이터 도구 실패는 제품 실패와 구분한다.
신규 동일 실패는 D17 최대3회 수정·관련 검사만 재실행, 통과 검사는 변경 영향/새
실패가 없으면 반복하지 않는다. Native sandbox 조사/운영 연동은 재개하지 않는다.

기능 완료선: 명시 fixture 설정의 검증/연결 실제 PASS, 필요한 새 검사와 독립 검토
PASS, 코드/근거 보존과 Draft PR 게시/원격 bytes 대조. 실제 소유자 값/운영 설치의
인수, supervisors/issuer/서명/required-check 연결은 남은 조건으로 보고한다.
