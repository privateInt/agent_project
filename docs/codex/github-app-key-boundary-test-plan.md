# Worker의 App 키 경로 open 거부 사전 검사 계획

계획 v1 / 2026-10-15. 기준 부모 `414ca0654ff07f14a1224faaf463be7a0c0818bc`, branch `codex/github-app-key-boundary-20261015`. 새 worktree 시작 clean을 확인했다. [GitHub 기준](common/GITHUB_GATES.md), [준비 계획](common/PREPARATION_TEST_PLAN.md)의 G06, [검사 기준](common/TESTING.md)의 D29/D30/D38을 유지한다. 독립 사전 검토 전 source/test를 구현하지 않는다.

범위는 새 source `src/ops/tools/github_app_key_access_probe.py`, 새 test `src/ops/tools/tests/test_github_app_key_access_probe.py`, 이 계획, 새 guide `docs/codex/github-app-key-boundary.md`, 새 소형 근거 `docs/testing/github-app-key-boundary-offline-20261015.json`과 기존 `docs/status.md` 준비표의 **안내 행 하나**인 총6파일이다. root는 source/guide/status, test_agent는 test/근거, reviewer는 사전·최종 검토를 맡는다. 공통 정책·기존 source/test/근거는 그대로 두고 기존20/46 suite와 기존 변이는 반복하지 않는다.

## 적절성과 완료선

이번 probe는 지정 invocation의 지정 경로에서 **읽기 전용 open이 EACCES로 거부됐는가**만 관찰한다. 공개 control을 먼저 확인하고 key open을 최대1회 시도한다. 열려도 즉시 close를 시도하며 key byte·metadata/stat·fingerprint를 읽지 않는다. 부모 디렉터리 권한 때문에 존재하지 않는 경로도 EACCES가 될 수 있으므로 key_presence_verified=false는 필수다. key_owner는 비교할 OS 계정 pin이지 실제 파일 소유자 증명이 아니다.

이는 실제 emitter나 완성된 credential store/sandbox의 인수가 아니다. 키 신원·타 경로·부모 symlink·supplementary groups/capabilities·향후 privilege escalation·namespace/runtime 격리·trusted emitter/model 접근·독립성/binding/gate/merge는 미검증이다. 기존 App metadata의 사용자 관찰은 별도 근거로 유지하며 현재 키/등록 상태를 추정해 키 생성·교체를 요구하지 않는다. 공개 문서에 비공개 사건·개인 경로·첨부 내용 추론을 넣지 않는다.

## 동결 계약

| 대상 | 계약 |
|---|---|
| 실행/API | Linux Python3.10 stdlib 단일 파일. `probe(worker_user,key_owner,key_path)->dict`, `main(argv=None,output_stream=None)->2`; 내부 helper는 구현 책임. 외부 evidence/UID/transport를 받아 판정하지 않고 실제 pwd·실행 identity·os.open을 내부 조회. 검사만 module reference를 monkeypatch. getres*/필수 Linux 상수 부재는 BLOCKED이며 더 약한 identity/open 방식으로 fallback하지 않음 |
| CLI | argparse 없이 정확6토큰: `--worker-user VALUE`, `--key-owner VALUE`, `--private-key VALUE` 각1회, 순서자유. 공백 분리만. =형식/축약/미지 flag/중복/누락/--help는 ARGUMENTS_INVALID. 출력 가능하면 허용 JSON 한 개, stderr 원문0, 항상 exit2. stdout write/flush/close 오류도 traceback0 |
| 값 | 유저명 각각 `[a-z_][a-z0-9_-]{0,31}`. 경로는 str·UTF-8<=4095bytes·절대경로·os.path.normpath와 정확히 동일, 빈값/NUL/제어문자·leading `//`·`..` component 거부. trim/경로/계정명 자동 보정 없음. 잘못된 값은 pwd/control/key 접근0 |
| 계정 pin | pwd에서 두 계정을 조회. 두 pw_uid와 pw_gid는 bool이 아닌 양의 int이고 worker/key-owner UID는 다름. root/동일UID/lookup 실패는 IDENTITY_PINS_INVALID. 실행 여부는 `os.getresuid()` 세 값 전부 worker UID, `os.getresgid()` 세 값 전부 worker primary GID와 일치해야 함. saved root도 불일치. 불일치는 EXECUTION_IDENTITY_MISMATCH, control/key open0 |
| 공개 control | identity 통과 뒤 `/dev/null`을 O_RDONLY\|O_CLOEXEC\|O_NONBLOCK로 정확1회 open·close. open 실패 CONTROL_OPEN_FAILED, close 실패 FD_CLOSE_FAILED, 둘 다 key open0. control_open_verified는 open/close 모두 성공한 뒤만 true |
| key open | control 완료 뒤 O_RDONLY\|O_NOFOLLOW\|O_NONBLOCK\|O_CLOEXEC open 최대1회. 시도 직전 counter 증가. **open에서 발생한** 정확 errno.EACCES만 KEY_PATH_OPEN_DENIED/OBSERVED. ENOENT/EPERM/ELOOP/그 외는 KEY_PATH_UNCONFIRMED/BLOCKED. 성공한 FD는0도 유효하며 즉시 close1회 시도 후 KEY_PATH_OPEN_ALLOWED/BLOCKED. close의 EACCES를 포함한 실패는 FD_CLOSE_FAILED/BLOCKED로 별도 처리; open 거부로 분류하지 않음. close 성공을 무조건 보장하지 않고 프로세스 종료가 최종 회수임을 명시 |
| 금지 동작 | key read/stat/서명·openssl·network/API·추론·설정/계정/sudoers 쓰기·worker 기동·서비스/보호/merge/sandbox 변경0. 제품은 경로 파일을 생성/변경하지 않는다. 알려진 계정/실제 키경로를 일반 코드에 하드코딩하지 않음 |
| native 안내 | 검토 commit/source SHA를 고정한 단일 파일을 기존 sudo/runuser 관리 경로로 지정 worker UID에서1회 실행할 명령만 준비. 실제 서버/키 접근은 이번 개발 환경에서0. 새 계정·sudoers·권한 변경·상시 worker 설치 없음. 실제 실행 결과는 후속 인수이며 현재 NOT_RUN |

report는 정확14키: `scope,status,reason,worker_identity_verified,control_open_verified,key_open_attempts,key_path_open_denied,key_bytes_read,key_presence_verified,privilege_escalation_verified,runtime_isolation_verified,trusted_gate_verified,native_ready,merge_authorized`.

scope=`WORKER_KEY_PATH_OPEN_ONLY`, status=OBSERVED/BLOCKED, reason은 ARGUMENTS_INVALID/IDENTITY_PINS_INVALID/EXECUTION_IDENTITY_MISMATCH/CONTROL_OPEN_FAILED/KEY_PATH_OPEN_DENIED/KEY_PATH_OPEN_ALLOWED/KEY_PATH_UNCONFIRMED/FD_CLOSE_FAILED/UNEXPECTED_FAILURE만 허용한다. identity/control/denied는 bool, attempts는 bool 아닌 int0..1, key_bytes_read는 bool 아닌 int0, 마지막6개 검증 flag는 언제나 false다. OBSERVED는 identity/control 통과 후 key open EACCES일 때만 허용한다. reason·출력 key/type/count/flag에 임의 값·raw argv/path/user/UID/GID/errno/예외를 섞지 않는다. 내부의 예상 밖 예외는 generic UNEXPECTED_FAILURE다.

## 필수 검사 선정

아래14개 주제를 methods/subtests와 일회성 문서 audit으로 작게 묶는다. timeout을 기다리거나 기존 제품 suite를 넓히지 않는다. 실제 canary는 비밀 없는 임시 공개 문자열 파일이며 운영 키가 아니다.

| ID | 상황·입력 | 기대 결과 |
|---|---|---|
| K01 | 정상 CLI 순서·invalid flag/중복/누락/=형식/--help·직접 API 타입 | 정확한3쌍만 수용. 오류는 sanitized ARGUMENTS_INVALID/exit2, lookup/control/key0 |
| K02 | 유저명·canonical 절대경로·UTF-8 경계·NUL/제어/비정상 Unicode·`..`/`//` | 자동보정 없이 거부, 값 echo0·open0 |
| K03 | lookup 실패·UID/GID bool/0·root·다른이름 같은 UID | IDENTITY_PINS_INVALID, control/private open0. 같은 primary group만으로 두 UID를 같다고 오판하지 않음 |
| K04 | real/effective/saved UID 또는 GID 하나씩 불일치·saved UID0 | EXECUTION_IDENTITY_MISMATCH, control/key0. 정상 계정 pin/control/key fixture가 다른 guard로 M2를 가리지 않음 |
| K05 | `/dev/null` 성공 및 open 실패·close 실패 | 정확 flags/호출1회/닫힘 순서. control 실패면 private open0, close 실패 보장 과장0 |
| K06 | EACCES key open, fd 반환 대신 OSError | OBSERVED/DENIED, identity/control true, attempt1/read0·6false. key open 호출1회·다른 path0 |
| K07 | ENOENT/EPERM/ELOOP/그 외 OSError·일반 예외 | missing/unknown을 deny 성공으로 승격하지 않고 BLOCKED. M1은 identity/control 정상·ENOENT인 유효 target |
| K08 | key open 성공 fd0/양수·close EACCES/그 외 실패 | 즉시 close1회 시도·read/stat0. 성공적으로 열렸으면 BLOCKED, close 오류는 항상 FD_CLOSE_FAILED이며 open EACCES로 승격0 |
| K09 | 정상/모든 실패 report·임의 내부 오류 | exact14/enum/strict bool/int/count·6false 고정. stdout JSON 외·stderr/raw/traceback0 |
| K10 | private sentinel을 path/user/lookup/open/close/출력 예외에 삽입·broken pipe | raw 비노출·항상exit2, 출력 불가를 정상 JSON 전송 성공으로 주장하지 않음 |
| K11 | 실제 임시 canary: 서로 다른 기존 nonroot owner/worker, child setgroups/setresgid/setresuid | control 성공 뒤0600 canary 경로 EACCES·OBSERVED. source/user pin·실행 real/effective/saved 일치. 부모/child cleanup, 운영키0 |
| K12 | 같은 두 UID의 실제 읽기 허용 canary, 입력과 identity는 정상 | open 성공을 KEY_PATH_OPEN_ALLOWED/BLOCKED로 판정, canary bytes read0. K11의 denial 대역 성공과 구분 |
| K13 | source standalone `python3 -I -B`·AST3.10·stdlib·제품 호출 surface | project import·read/stat/외부실행/network/쓰기 없음. 실제3.10 실행 여부 별도, source 계약과 read0 확인 |
| K14 | 일회성 guide/status/6파일 Git·문서 audit | guide는 placeholder/pinned source·worker 실행1회/exit2·EACCES와 존재/신뢰 분리·새 키/계정/권한/활성화 요구0. status 안내행1개만, App metadata 사용자관찰/이 도구 dev검사/native NOT_RUN·trusted/worker 미인수 구분. 전체6파일 금지정보0, 허용status행 외 기존byte불변, 링크/JSON/whitespace·검사수/시간/hash/PASS/FAIL/ERROR/SKIP 기록. reusable unit에 clean worktree 강제0 |

K11/K12는 local UID0만으로 가능하다고 단정하지 않는다. 기존 두 nonroot 계정, 임시 경로 chown·UID/GID 전환 권한과 도구를 확인하고 가능할 때 실제 실행한다. child의 setgroups/setresgid/setresuid 실패에는 root로 fallback하지 않고 실행을 중단한다. 계정 추가나 시스템 권한 확대는 하지 않는다. 선행조건 부족이면 이유를 기록하고 해당 실제 연동은 NOT_RUN/BLOCKED로 분리하며 mock PASS나 SKIP을 실제 canary 성공으로 세지 않는다. 사용자 서버 키의 실제 인수는 local canary 성공과도 별개다.

## D38 선정과 종료

reviewer가 두 의미 오류를 선정했다. M1은 **ENOENT를 EACCES와 같이 수용**하는 오류다. identity와 control은 정상, key는 ENOENT여서 정상 BLOCKED→변이 OBSERVED assertion FAIL이 되어야 한다. M2는 **실행 identity guard 제거**다. 계정 pin은 서로 다른 nonroot, real/effective UID는 worker지만 saved UID만0, GID는 모두 정상, control 성공/key EACCES를 준비한다. 정상은 open0/BLOCKED, 변이는 금지된 open 진행 또는 잘못 OBSERVED를 assertion FAIL로 검출한다. 다른 validation·문법/import 실패를 검출 성공으로 세지 않는다.

관련 새 검사와 선정된 두 격리 변이만 실행한다. 정상 target PASS·변이 assertion FAIL/ERROR0, 실제 canary/대역/미실행 구분, 실행 전후 source/test/계획 hash, mutant hash·원본 불변·복사본 삭제를 소형 근거와 독립 최종 검토에 연결한다. 새 변경/실패 없이 반복·확대하지 않는다. 과거 sandbox EACCES 문제와 D17/D18 보류·누적/소진은 재개·초기화하지 않는다. 종료는 **이 좁은 syscall 관찰 도구의 격리 검증과 후속 native 명령 준비**이며 trusted emitter/무인 개발/병합 준비 완료가 아니다.

공식 API 참조: [Python3.10 os](https://docs.python.org/3.10/library/os.html). root가 2026-10-15에 getresuid/getresgid의 real/effective/saved와 open/close·플랫폼 상수 가용성을 확인했다. 실제3.10 실행 검증과는 구분한다.
