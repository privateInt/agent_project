# 고정 fixture controller

PR #13 `e1eff75b47cdf3fae1088d51d35aaea701f81477`의 수집기와 원장 API를
그대로 연결한다. 고정 fixture 전용이며 실제 head·서명·검사 발행·worker 설치는
범위 밖이다. 사전 계획 C01-D29-v1 SHA-256은
`227df66ea111e1d29e5982290630deac9b3b40b9e034e064a08283540142962e`이고,
독립 사전 검토의 계약·상태 우선순위와 D38 두 대상을 따른다.

호출자는 별도 전용 SQLite connection 두 개를 준비한다. 기존 원장은 원래 API로,
control DB는 `initialize_fixture_controller(control_connection)`으로 명시 초기화한다.
control DB는 application ID `1095779907`, version1, 단일
`fixture_controller_jobs` table이다. 기존 원장 schema1은 변경하지 않는다.
경로·연결 생성·자동 초기화·migration·재시도·CLI는 제공하지 않는다.

`run_fixture_job(ledger_connection, control_connection, state_dir_fd,
expected_assignment_sha256, assignment_bytes)`는 기존 원장의 불변 assignment를
확인하고, 새 control claim의 COMMIT 응답이 확인된 호출에서만 수집한다.
기존 CLAIMED는 재실행 권한이 아니다. 수집 중 transaction을 유지하지 않는다.

수집기의 정확한 응답에서 실행 전임이 확실한 6개 reason만
PREPARATION_FAILED로 영속화한다: ARGUMENTS_INVALID, STATE_FD_INVALID,
ASSIGNMENT_FILE_INVALID, ASSIGNMENT_HASH_MISMATCH, ASSIGNMENT_INVALID,
REAL_EXECUTION_BLOCKED. SPAWN_FAILED·정리/IO/예상 밖 오류는 불명으로 남긴다.
terminal 실패는 디렉터리가 고쳐져도 재수집하지 않는다.

`read_fixture_controller_job(ledger_connection, control_connection, job_id,
expected_assignment_sha256)`는 쓰기·실행 없이 원래 job을 조회한다. CLAIMED만
있으면 OUTCOME_UNKNOWN, 확정 준비 실패는 PREPARATION_FAILED, 검증된 저장
artifact는 ARTIFACT_STORED다. 실패와 artifact가 동시에 있거나 control pin이
다르면 STATE_CONFLICT이며 자동 복구하지 않는다. 잘못된 caller pin에는
ASSIGNMENT_PIN_MISMATCH와 함께 record/artifact를 반환하지 않는다.

DB/COMMIT 응답 오류는 DATABASE_UNCONFIRMED다. artifact 저장 응답이 유실되면
원래 artifact bytes를 caller에 반환하며 저장 완료로 선언하지 않는다. 이후 별도
조회로 실제 commit을 확인한다. 자동 재수집/새 job/재전송은 없다. claim이 실제로
없다는 별도 조회 뒤 새 명시 run은 허용하지만, 이미 저장된 claim은 재사용하지 않는다.

반환은 `{report, record, artifact}`다. record는 기존 원장 record 또는 None,
artifact는 원래 bytes 또는 None이다. report는 고정 20키·15 reason만 허용한다.
CONTROLLER_READY/ARTIFACT_STORED만 OBSERVED이고 나머지는 BLOCKED다.
fixture_only=true/head_code_executed=false이며 기존 9개 신뢰 flag는 항상 false다.
저장 확인은 실행·검사 성공이 아니다. module은 stdout/stderr에 원문을 출력하지 않는다.

두 DB의 원자적 commit, 영구적 DB pairing, 실행 exactly-once, 경로/owner/roster
출처, native 격리·신뢰 gate는 증명하지 않는다. 실제 연결에는 보호된 설치·설정
로더, 역할별 supervisor/issuer와 최신 대상 binding의 별도 인수가 필요하다.
