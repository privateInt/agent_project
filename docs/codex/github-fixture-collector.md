# 고정 진단 작업의 실행 근거 수집

이 단계는 [서명 검증기](github-signed-receipts.md)에 공급할 근거를 parent가 직접 생산하는 흐름을 구현한다. 공개 함수 `collect_fixture_assignment(state_dir_fd, expected_assignment_sha256)`는 assignment 로드 → 소스에 고정된 진단 실행 → 실제 pipe/종료 관찰 → artifact bytes와 최종 binding 생성까지 수행한다. [사전 검사 계획](github-fixture-collector-test-plan.md)과 [개발 근거](../testing/github-fixture-collector-offline-20261015.json)를 함께 확인한다.

## 실행 전 대상과 수집 후 결합

새 실행 artifact의 hash를 실행 전에 알 수는 없다. 따라서 owner/controller는 먼저 `repository`, `pr_number`, `base_sha`, `head_sha`, `release_sha256`, `plan_sha256`의 **target 6필드**와 job·실행 한도를 assignment에 고정한다. 수집 후 실제 artifact bytes의 SHA-256을 `evidence_sha256`로 붙여 기존 검증기가 받을 7필드를 완성한다. artifact 안에는 target6만 포함하며 자기 hash·최종 7binding·receipt를 넣지 않는다. 같은 job의 artifact를 owner ledger에 한 번 결합하는 실제 저장·재개·CAS는 후속이다.

target의 `release_sha256`는 기존 공통 규칙 배포판 digest다. collector/evaluator 설치 코드의 release digest와 같은 뜻이 아니다. 실제 controller 설치 시 별도의 검토된 코드 pin을 대조해야 한다. 이번 함수가 자신의 소스 파일 hash를 읽었다는 사실로 active runtime이나 설치 출처를 인증하지 않는다.

## FD와 assignment 경계

호출자는 이미 연 디렉터리 FD와 별도 기대 assignment SHA-256을 전달한다. 함수는 자기 소유의 CLOEXEC duplicate를 만들어 실제 디렉터리·현재 effective UID 소유·정확 mode0700을 확인하고, 그 FD를 기준으로 고정 이름 `assignment.json`을 직접 연다. `O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC`을 사용하며 regular file·같은 UID·정확 mode0600·link1·1..16,384 bytes를 요구한다. bytes digest와 엄격한 UTF-8/JSON·shape를 확인하고 모든 자체 FD를 닫은 뒤에만 child를 실행한다. caller의 FD와 offset은 보존한다. close 오류 뒤 같은 번호를 재시도하지 않는다.

디렉터리 FD를 받았다는 사실로 그 원래 경로·상위 디렉터리·PR 밖 위치·owner 의도·기대 hash 출처가 증명되지는 않는다. native controller는 별도로 보호된 anchor와 설치 경로를 확인해야 한다. 실제 inode/FD/mode/digest 대조와 config 출처 인증을 구분한다. 마지막 `assignment.json` symlink는 직접 거부하지만 이미 caller가 연 디렉터리의 원래 symlink 여부를 추정하지 않는다.

assignment는 정확히 아래 7키다. unknown/duplicate key, 비유한 수, 깊이8 초과, 20자 초과 정수, BOM·잘못된 UTF-8을 거부한다.

| 필드 | 값 |
|---|---|
| `schema` | bool이 아닌 정수1 |
| `mode` | `fixture`. `head` 요청은 Popen 전에 `REAL_EXECUTION_BLOCKED` |
| `job_id` | ASCII `[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}` |
| `target` | 위 exact6필드. 기존 엄격한 binding validator를 임시 evidence 값으로 재사용하며 그 임시값을 산출물에 저장하지 않음 |
| `fixture_profile` | 아래 고정 profile 중 하나 |
| `timeout_ms` | strict int100..2000 |
| `max_stream_bytes` | strict int1..65536. stdout/stderr 각각의 captured-prefix 한도 |

## 실행과 실제 관찰

| Profile | 검토 소스에 고정된 동작 |
|---|---|
| `ok` | 짧은 stdout/stderr marker 후 exit0 |
| `nonzero` | stdout에 PASS 문구를 쓰고 exit7. 그 문구는 판정 입력이 아님 |
| `dual-output` | stdout 32768 bytes와 stderr 32768 bytes를 기록 |
| `wait` | 10초 대기. assignment의 최대2초 deadline에서 중단 |
| `closed-pipes-wait` | stdout/stderr를 닫은 뒤 10초 대기. EOF를 프로세스 성공으로 오인하지 않는 검사 |
| `signal` | 자기 프로세스에 SIGTERM 전송 |

어떤 profile도 descendant를 만들지 않는다. 임의 argv·코드·환경·launcher·이미 실행한 Popen 객체·PR helper를 입력으로 받지 않는다. source의 고정 literal을 `/usr/bin/python3 -I -B -c`로 실행한다. cwd는 `/`, env는 `LC_ALL=C`/`PATH=/usr/bin:/bin`만, stdin은 DEVNULL, stdout/stderr는 unbuffered PIPE, `close_fds=True`, `pass_fds=()`, `start_new_session=True`다. assignment FD는 child에 상속하지 않는다.

parent가 두 pipe를 selectors로 동시에 읽고 monotonic deadline과 각 stream 한도를 적용한다. raw stdout/stderr를 공개 report나 artifact에 복사하지 않는다. captured byte count와 SHA-256, EOF·prefix 여부를 기록한다. 한도를 넘는 1byte를 실제로 관찰하면 읽은 prefix만 hash한 것으로 표시하고 중단한다. timeout·overflow·IO/정리 불명은 완전한 출력이 아니다. 정상 EOF·실제 종료/reap 여부와 exit0 관찰을 각각 구분한다. 완전히 수집한 exit7·signal도 실패한 child를 관찰한 근거이며 검사 PASS가 아니다.

중단 시 직접 child에 kill을 시도하고 최대1초 wait로 reap을 확인하며 pipe/selector를 정리한다. 고정 profile의 direct child 정리 범위이며 임의 head의 daemon·process group·descendant 전체 정리 보장이 아니다. 실제 head launcher에는 별도 UID/권한·FD/env·caps/ptrace/proc·network와 descendant 거부 인수가 필요하다.

## 산출물과 완료선

반환은 `{report, artifact, binding}`이다. artifact는 canonical JSON의 원래 bytes, binding은 그 bytes hash를 붙인 exact7필드다. assignment/실행 준비가 잘못되면 artifact와 binding은 None이다. 수집 중 실패하면 실패·불완전성을 담은 artifact를 만들 수 있다. artifact에는 fixture kind, assignment digest/job/target6, 실제 전달한 고정 profile code digest, 한도와 parent가 관찰한 exit/출력 metadata만 포함한다. code digest는 전달 bytes의 관찰이며 설치/active runtime 출처 인증이 아니다.

fixture 결과를 필수 검사나 독립 review PASS로 사용할 수 없다. `fixture_only=true`, `head_code_executed=false`이며 실제 출처·상태 anchor·실행 격리·issuer·독립성·trusted gate·native 준비·병합 승인은 항상 미검증이다. 서명·App 키를 읽거나 receipt를 발행하는 단계도 아니다. 같은 UID의 고정 fixture 실행은 native head 격리 인수를 대신하지 않는다.

후속은 보호된 controller/assignment ledger와 검증된 worker launcher, 실제 tests/reviewer supervisor/issuer 연결이다. 기존 sandbox EACCES 세 실패·D17/D18 상한·연장 소진에 따른 보류는 그대로 유지한다. 이 fixture 경로를 head 실행으로 바꾸는 flag나 fallback은 제공하지 않는다.
