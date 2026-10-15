# Worker의 App 키 경로 접근 거부 확인

2026-10-15. [인증 metadata 관찰](github-app-readback.md) 다음의 작은 준비 단계다. [사전 검사 계획](github-app-key-boundary-test-plan.md)에 따라 [단일 파일 probe](../../src/ops/tools/github_app_key_access_probe.py)를 준비한다. 개발 환경의 검사와 실제 서버 인수는 [격리 검사 근거](../testing/github-app-key-boundary-offline-20261015.json)에서 구분한다. 현재 사용자 서버의 이 probe 실행은 **NOT_RUN**이다. 현재 상태와 진입점은 [상태표](../status.md)에서 관리한다.

## 확인하는 범위

실제 지정 worker 계정에서 공개 control을 연 뒤 지정 키 경로의 읽기 전용 `os.open`을 한 번 시도한다. 키 본문·stat·fingerprint는 읽지 않는다. 열렸다면 즉시 close를 한 번 시도하고 BLOCKED로 보고한다. **key open 자체의 errno가 EACCES인 경우만 OBSERVED**다. 파일 없음(ENOENT)·EPERM·symlink 오류(ELOOP)·그 밖의 오류는 KEY_PATH_UNCONFIRMED/BLOCKED다. open 성공 뒤 close가 EACCES로 실패해도 접근 거부 성공으로 바뀌지 않는다.

이 관찰은 그 프로세스·그 시점·그 경로의 open 거부에 한정된다. 부모 디렉터리에서 접근이 막히면 존재하지 않는 파일도 EACCES일 수 있어 `key_presence_verified`는 항상 false다. `--key-owner`는 비교할 계정 pin이며 실제 파일 소유자·키 신원을 인증하지 않는다. 소유자 셸의 별도 존재 확인과 worker의 거부도 같은 inode에 대한 원자적 증명이 아니다.

real/effective/saved UID와 GID를 대조해도 supplementary groups·capabilities·다른 경로/키 사본·부모 symlink·향후 권한 상승·namespace/runtime 격리는 미검증이다. 기존 [GitHub 기준](common/GITHUB_GATES.md)의 trusted emitter·독립 reviewer·binding·필수 검사·보호/병합 인수는 남는다. 기존 sandbox EACCES 조사 보류와 D17/D18 누적·소진도 유지하며 이 도구가 Codex sandbox를 재실행하지 않는다.

## 실행 계약

Linux/Python3.10 이상의 stdlib만 사용한다. 제품의 외부 실행·GitHub/API/모델 요청·서명·설정/계정/권한 변경·서비스/worker 기동은 없다. 계정 pin 조회는 OS의 `pwd.getpwnam`을 사용하며 OS가 구성한 NSS 동작이나 조회 시간을 별도로 증명하지 않는다. 전체 실행 제한은 외곽 timeout으로 둔다.

CLI는 다음 세 flag/value 쌍을 각각 한 번씩, 공백으로 분리해 받는다. 순서는 자유다. 아래 표의 값은 placeholder이며 단독 실행 명령이 아니다. `=` 형식·축약·중복·미지 flag·누락·`--help`는 ARGUMENTS_INVALID/exit2다.

| 인자 | 값 |
|---|---|
| `--worker-user` | 기존 격리 worker 계정 이름 |
| `--key-owner` | 기존 키 보관 계정 이름 |
| `--private-key` | 소유자 셸에서 확인한 기존 키의 canonical 절대경로 |

두 계정의 UID/GID는 양의 정수이고 UID는 서로 달라야 한다. 이름은 `[a-z_][a-z0-9_-]{0,31}`이며 root·동일 UID·없는 계정은 차단한다. 실행 프로세스의 real/effective/saved UID 세 값 모두 worker UID, GID 세 값 모두 worker primary GID와 일치해야 한다. getres 함수나 필수 Linux 상수가 없으면 더 약한 방법으로 대체하지 않는다.

identity 통과 후 `/dev/null`을 O_RDONLY|O_CLOEXEC|O_NONBLOCK로 한 번 열고 닫는다. 양쪽 모두 성공한 경우만 control_open_verified=true다. control 실패에는 key open을 시도하지 않는다. key는 추가 O_NOFOLLOW로 최종 경로 요소의 symlink 따라가기를 막고 O_NONBLOCK로 열며 읽기·서명·파일 생성은 하지 않는다. 부모 경로 전체의 symlink를 막는 계약은 아니다. close 오류는 FD_CLOSE_FAILED/BLOCKED이며, 실패한 close를 재시도하거나 회수를 보장하지 않는다. CLI 프로세스 종료가 최종 FD 회수 경계다.

## 서버에서 한 번 실행할 순서

1. 기존 키 보관 계정의 셸에서 기존 key 경로가 regular·nonsymlink·자기 소유인지 확인한다. 키 내용이나 사본을 공유 폴더에 넣지 않는다. 기존 worker/보관 계정이나 관리 실행 경로가 없으면 BLOCKED로 남기며 새 계정·sudoers·키 권한을 만들거나 바꾸지 않는다.
2. 게시된 검토 commit과 source SHA-256을 고정해 **공개 probe 파일만** 임시 디렉터리에 내려받아 검증한다. worker가 읽고 실행할 수 있도록 공개 디렉터리만 mode0555, probe만0444로 준비한다. 키 디렉터리의 mode0700/파일0600 또는0400은 유지한다. 이 단일 검토 파일을 수동 실행하며 PR head 전체를 보관 계정에서 실행하지 않는다.
3. 기존 관리 경로의 `sudo -n -u WORKER_USER --` 등으로 `/usr/bin/python3 -I -B`의 이 파일을 worker UID에서 한 번 실행한다. 안쪽 `timeout --kill-after=2s 12s`, 바깥쪽 `timeout --kill-after=2s 20s`를 사용한다. sudo 허용 실패/시간 초과는 probe 관찰 성공이 아니다. 자동 재시도·상시 실행은 없다. 제품 자체는 계정 전환을 실행하지 않는다.
4. stdout의 JSON과 관찰된 종료 코드만 공유한다. probe의 정상 출력이 OBSERVED여도 기대 exit는 **2**다. exit2만으로 성공을 판단하지 않고 scope/reason·identity/control/attempt/denied를 함께 확인한다. 결과를 받은 뒤에만 새 사용자 서버 관찰로 기록한다.

실제 검토 commit/SHA를 넣은 명령은 게시 후 전달한다. 내려받기·파일 공개·sudo/timeout 실행·정리는 전달 명령의 수동 준비 동작이며 제품 API의 동작과 구분한다. 계정 전환 실패 시 보관 계정이나 root로 probe를 대신 실행하지 않는다. 키가 열리는 결과가 나오면 권한이나 기대값을 바꾸어 성공으로 만들지 않는다.

## 결과 읽기

정상 관찰은 `scope=WORKER_KEY_PATH_OPEN_ONLY`, `status=OBSERVED`, `reason=KEY_PATH_OPEN_DENIED`, `worker_identity_verified=true`, `control_open_verified=true`, `key_open_attempts=1`, `key_path_open_denied=true`, `key_bytes_read=0`이다. 총14필드의 허용된 JSON 하나만 출력하며 사용자명·UID/GID·경로·키·예외 원문은 출력하지 않는다. 출력 채널 오류로 JSON을 전송하지 못한 경우를 관찰 완료로 보지 않는다.

`key_presence_verified`, `privilege_escalation_verified`, `runtime_isolation_verified`, `trusted_gate_verified`, `native_ready`, `merge_authorized` 여섯 flag는 항상 false다. 잘못된 값/계정 pin/실행 identity이면 control/key 접근은 0회다. 뜻을 알 수 없는 실패는 generic BLOCKED로 남긴다. App metadata 성공이나 이 syscall 거부 관찰을 전체 credential 격리·무인개발·병합 허가로 승격하지 않는다.

공식 참조: [Python3.10 os](https://docs.python.org/3.10/library/os.html), 2026-10-15 열람. getresuid/getresgid는 real/effective/saved 값을 반환하고 os.open/close는 별도 호출이다. 상수·함수의 플랫폼 가용성과 실제 서버 인수는 구분한다.
