# U01a 순수 admission·다음 작업 선택기 delta 계획

ID: **U01a-D29-v1**. 별도 test_agent 작성, 독립 사전 검토 대기.
기준 [검토](agent-unattended-plan-review.md) SHA:
`498f7eda206c8b6f2c04b4d6626975f8383dfc441c1616a7ebc00d6d46aeca29`.
기존 [U01 계획](agent-unattended-plan.md) SHA `57c43182…`와 runtime/transport 원문·첫 실패를
변경하지 않는다. 이 계획 작성 중 구현·검사·runtime action은 수행하지 않았다.

## 범위·기존 코드 재사용

새 파일 후보: `src/ops/tools/codex_unattended_admission.py`, 인접 새 검사 한 파일.
외부가 제공한 config/snapshot/state를 검사해 **후보와 설정 제안만** 반환한다.
queue/turn/settings IO, journal/CAS/fsync 구현, socket/WebSocket, credential, filesystem 조회,
clock 조회, scheduler entrypoint/서비스 설치·PAUSE 변경은 없다. U01b의 연결·내구성 구현과
실제 두 작업은 제외한다. vms_project와 원래11개 변경/기존 jobs3는 접근·수정하지 않는다.

`/opt/codex-agent/scheduler-v2/flex_gate.py`의 **validate_config와 windows만** 재사용한다.
원문 SHA `30419990ee0cd77dfe37923ac6fe051b6b47b94469a39cdf00452291b5daf16b`.
그 원문은 유지하고 framework 전체를 복사하지 않는다. caller/검사 setup이 그 고정 모듈을
준비한 뒤 두 순수 함수만 keyword dependency로 주입한다. decide/read_config/safe_json_file/
main은 호출하지 않는다. 함수 실행 중 importlib/파일 로딩도 하지 않는다.

## 정확한 공개 API와 값 계약

`select_next_task(config, snapshot, state, *, schedule) -> dict`

schedule은 `(validate_config_callable, windows_callable)`인 신뢰한 고정 dependency다.
나머지 입력은 JSON-compatible builtin dict/list/scalar만이며 exact keys·strict type을
검사한다. SHA는 lowercase40/64 hex, ID는 비어 있지 않은128자 이내 ASCII 식별자다.
경로는 절대 정규 문자열이고 `.`/`..`/중복 slash/NUL을 거부한다. 실제 realpath/owner/출처를
확인했다는 뜻이 아니다. 입력과 nested 값은 변경하지 않는다. invalid 입력은 고정 BLOCKED
결과이며 raw exception/path/내용을 출력하지 않는다. 같은 입력은 같은 결과를 반환한다.

config의 exact keys:

```text
schema=1, batch_id, project, freshness, schedule, tasks
project = {project_id, repository, dev_root, git_common_dir, base_sha, allowed_writable_roots}
freshness = {max_age_ms}
task = {task_id, predecessor_id, plan_sha256, required_test_ids, allowed_files, timeout_sec}
```

project_id는 literal agent_project다. repository는 검토된 owner/agent_project 문자열이다.
dev_root/common-dir/base는 외부 검토 config의 정확 값이며 wildcard·자동 발견이 없다.
allowed_writable_roots는 dev_root와 기존 명시 임시 영역의 정확 리스트다. tasks는 최대32개,
중복 ID 없음, 첫 predecessor_id=None, 이후는 바로 앞 task ID인 순서다. required_test_ids와
allowed_files는 비어 있지 않고 중복 없는 리스트다. 파일은 정규 상대 경로만 허용한다.
timeout_sec은 strict int30..7200. task의 최초 base는 project.base_sha이며 후속 base는 검증된
predecessor의 게시 head다. backlog에 세 번째 후보를 표현할 수 있지만 batch admission은
평생 최대2다. 세 번째 실제 작업 승인/제출을 뜻하지 않는다.
schedule 값은 기존 flex_gate schema version2를 그대로 사용하며 implicit default는 없다.

snapshot의 exact keys는 `now_ms, project, pauses, quota, control, settings`다.
now_ms는 외부 supplied UTC Unix milliseconds strict nonnegative int다. 나머지5개는 정확히
`{observed_at_ms, value}`이며 value의 exact keys는 아래다.

```text
project: {project_id, repository, dev_root, git_common_dir, base_sha, head_sha}
pauses: {root, user, state}                         # 각각 bool 또는 None
quota: {ordinaryUsageAllowed, exhausted}            # 각각 bool 또는 None
control: {available, desired_settings_supported}   # 각각 bool 또는 None
settings: {cwd, approvalPolicy, approvalsReviewer, sandbox, runtimeWorkspaceRoots}
sandbox: {type, networkAccess, writableRoots}
```

freshness.max_age_ms는 필수 strict int1..300000이며 default0/무한/환경 추정이 없다.
5개 observation 모두 **0 <= now_ms-observed_at_ms <= max_age_ms**여야 한다.
정확한 age 경계는 허용, 경계+1ms는 SNAPSHOT_STALE, 미래 timestamp는 SNAPSHOT_TIME_INVALID다.
관측 timestamp 누락/type 오류는 INPUT_INVALID다. state.clock_floor_ms보다 now가 과거인
clock rollback도 SNAPSHOT_TIME_INVALID다. root/user/state pause는 순서대로 먼저 처리하며 true 또는
None이면 차단한다. 모두 false인 경우에만 freshness를 포함한 뒤 predicate로 진행한다.
따라서 stale true pause도 보수적으로 차단하며 dev unpaused가 root/user pause를 덮지 못한다.

state의 exact keys는 `schema=1, batch_id, clock_floor_ms, admissions, evidence`다.

```text
admission = {task_id, project_id, base_sha, head_sha, plan_sha256, admitted_at_ms, status}
status = RESERVED | QUEUED | ACTIVE | COMPLETED | FAILED | INTERRUPTED |
         UNKNOWN | SUBMISSION_UNKNOWN | START_UNKNOWN | CHECKPOINT_UNCONFIRMED
```

task_id는 선언된 tasks의 중복 없는 ID이며 batch/project/plan/base 연결을 검사한다.
미완료 head_sha는 None, COMPLETED는40hex다. timestamp는 now보다 미래일 수 없다.
UNKNOWN 계열이 하나라도 있으면 후보 없음. RESERVED/QUEUED/ACTIVE는 occupied slot이며
하나면 ACTIVE_TASK, 둘 이상이면 STATE_CONFLICT다. 모든 admission은 실패/불명 포함 lifetime
count에 남으며 입력에서 환급하거나 task ID를 교체하지 않는다. lifetime은 len(admissions)다.
evidence는 admission의 task_id만 키로 갖는 아래 완료 bundle map이다.

출력 exact keys:

```text
{schema:1, scope:"PURE_ADMISSION_ONLY", status, reason,
 candidate, desired_settings, runtime_io_performed:false, mutation_authorized:false}
status = CANDIDATE | BLOCKED | NO_CANDIDATE
candidate = {task_id, project_id, repository, dev_root, git_common_dir, base_sha, plan_sha256}
```

BLOCKED/NO_CANDIDATE에서는 candidate/desired_settings가 None이다. 후보가 있어도 IO나
설정 변경을 승인하는 성공 flag는 없다. desired_settings는 정확히 다음 **미적용 제안**이다.

```json
{"approvalsReviewer":"auto_review","approvalPolicy":{"granular":{"sandbox_approval":false,"request_permissions":false,"rules":false,"mcp_elicitations":false,"skill_approval":false}}}
```

settings snapshot은 cwd/runtimeWorkspaceRoots가 dev_root와 정확히 일치하고 sandbox.type은
workspaceWrite, networkAccess는 bool false, writableRoots는 승인 리스트와 일치해야 한다.
approvalPolicy는 기존 on-request 또는 위 exact all-false granular, approvalsReviewer는
user 또는 auto_review만 받는다. 나머지 권한 확대/누락은 SETTINGS_UNVERIFIED다.
control.desired_settings_supported가 명시 true가 아니어도 SETTINGS_UNVERIFIED다.
이 입력 검사로 실제 server 지원·권한 강제·independent reviewer 출처를 인증했다고 말하지 않는다.
현재 parent/children의 on-request·reviewer 설정을 적용하거나 변경하지 않는다.

## T1→T2 완료 근거의 정확한 결합

후보는 아직 admission되지 않은 첫 task다. 첫 task는 현재 snapshot head=project.base_sha가
필요하다. 다음 task는 predecessor status=COMPLETED만으로 부족하며 아래 전체 bundle을 요구한다.

```text
B = {project_id, repository, batch_id, task_id, dev_root, git_common_dir,
     base_sha, head_sha, plan_sha256}
h = SHA256(UTF-8 JSON(B, sort_keys=True, separators=(",",":"), ensure_ascii=True))
bundle = {binding:B, terminal, tests, review, draft_pr, readback}
terminal = {binding_sha256:h, status:"completed"}
tests = {binding_sha256:h, results:{required_test_id:"PASS"}, failures:0, errors:0, skipped:0}
review = {binding_sha256:h, status, independent, implementer_id, reviewer_id}
draft_pr = {binding_sha256:h, url, draft, base_sha, head_sha, files_sha256}
readback = {binding_sha256:h, status, head_sha, files_sha256}
```

B는 config/state의 동일 project/task/batch/base/head/plan과 완전히 일치해야 한다.
tests.results의 key 집합은 해당 task의 required_test_ids와 같고, 전부 PASS이며
FAIL/ERROR/SKIP는 strict int0이어야 한다. review는 PASS/independent=true/서로 다른
비어 있지 않은 actor IDs를 요구한다. review/readback status는 PASS/FAIL/PENDING/NOT_RUN을
표현할 수 있으며 PASS 외 값도 유효한 미충족 입력이다. 위 bundle 표는 충족값의 명세다.
bundle section 누락·type/status 문제·결합 불일치는 EVIDENCE_UNVERIFIED로 처리한다.
Draft PR은 같은 repository의 정규 GitHub pull URL·draft=true·같은 base/head를 요구한다.
files_sha256은 allowed_files의 비어 있지 않은 subset이며 각 값은64hex다. readback은
PASS·같은 head·동일 files_sha256을 요구한다. snapshot current head도 게시 head와 같아야
하며 그 head를 T2 후보 base로 사용한다. 실패/불명·completed 문장·exit0만으로 선택하지 않는다.
이는 원래 ID/실패를 보존하는 bundle 구조·결합 검사이며 서명·OS/역할 독립성·출처 증명이 아니다.

## 판정 순서와 고정 reason

shape/type 오류는 INPUT_INVALID, 다른 project는 PROJECT_NOT_ALLOWED, project/root/common-dir/
base 불일치는 PROJECT_BINDING_MISMATCH다. 다음 root→user→state pause에서 true는 각각
ROOT_PAUSED/USER_PAUSED/STATE_PAUSED, 먼저 만난 None은 PAUSE_UNVERIFIED다.
그 후 SNAPSHOT_TIME_INVALID/SNAPSHOT_STALE→UNKNOWN_STATE→STATE_CONFLICT/ACTIVE_TASK 순이다.
schedule.windows의 가장 최근에 시작한 창을 사용한다. 없으면 OUTSIDE_WINDOW, 창 안의
admission 수>=기존 cap은 WINDOW_CAP_REACHED, lifetime>=2는 LIFETIME_CAP_REACHED다.
시작은 inclusive, 종료는 exclusive이며 date override/겹친 창은 기존 windows의 계산 그대로다.
잔여 시간이 task timeout+기존 margin120초보다 작으면 WINDOW_TOO_SHORT, 같으면 허용한다.
창 내 count는 admitted_at_ms로 계산한다. timestamp 미래/clock rollback은 SNAPSHOT_TIME_INVALID다.
다음 usage 명시 true 및 exhausted=false 외에는 USAGE_UNVERIFIED,
control.available!=true는 CONTROL_UNAVAILABLE, 설정 불명은 SETTINGS_UNVERIFIED다.
후보가 없으면 NO_PENDING_TASK, predecessor 미완료는 PREDECESSOR_NOT_COMPLETED,
bundle 미충족/결합 불일치는 EVIDENCE_UNVERIFIED, current head 불일치는 HEAD_BINDING_MISMATCH다.
모두 충족한 경우만 CANDIDATE_READY다. 이외 reason·자동 default·I/O·print를 추가하지 않는다.

## 신규 검사: 6 methods와 정확히2개 변이

| method | 필수 입력·기대 결과 |
|---|---|
| test_input_binding_and_purity | 정상 T1 후보, vms/다른 root/common/base/batch/plan 거부, strict bool/int/path/hash/중복ID. deep copy로 입력 불변 대조, 같은 순수 입력의 동일 결과, 선택 함수 안 file/socket/subprocess/clock IO0. schedule에는 위 pin의 실제 함수를 setup에서 한 번 로드 |
| test_pause_and_freshness | root/user/state 각각 true, root+user 동시, unknown, devfalse가 roottrue를 덮지 못함. 각5개 관측의 age0/max/max+1/미래/누락과 clock rollback. 정확한 경계 reason 및 candidate=None |
| test_schedule_quota_and_settings | 자정 넘김/시작/종료/date override/겹친 창/기존 window cap/여유120초의 ±1ms. quota false/null/exhausted/stale·control 불명·설정 불일치/unsupported 거부. 정상은 desired allfalse+auto_review만 반환, 기존 설정 변경0 |
| test_state_and_t1_t2_evidence | occupied1/2·UNKNOWN 모든 종류·실패 predecessor. 완전한 T1 bundle에서 T2만 선택. tests 누락/skip/다른 binding·review 비독립/FAIL·Draft PR 미완료/다른 repo·readback 미완료/hash/head/plan 불일치를 개별 거부. NO_PENDING도 확인 |
| test_lifetime_cap_blocks_third_candidate | **단일 사례/M1 target**. 창cap3, completed T1/T2와 두 완전 bundle, active0, 올바른 head, fresh gates, 세 번째 backlog 후보로 나머지 조건 모두 정상. lifetime2 때문에 LIFETIME_CAP_REACHED/candidate=None |
| test_t2_pending_review_blocks | **단일 사례/M2 target**. T1 완료와 readback을 포함한 나머지 조건은 모두 정상이고 review.status만 PENDING. EVIDENCE_UNVERIFIED/candidate=None으로 T2 거부 |

M1은 lifetime cap guard를 생략해 잘못된 세 번째 **후보**를, M2는 predecessor review/readback
완료 guard를 생략해 PENDING review에서 잘못된 T2 **후보**가 선택되는 것을 검출한다.
각 단일 target은 semantic FAIL1/ERROR0을 요구한다. 정상 target은 최초 PASS만 재사용한다.
dispatch/replay 변이는 추가하지 않는다. 다른 guard/fixture 오류가 먼저 거부한 결과를 변이
검출로 세지 않는다. 두 경우 모두 실제 세 번째 task·daemon IO는0이다.

D29 exact 계획 독립 승인 후에만 coordinator가 제품을, test_agent가 신규 검사를 작성한다.
새 module만 최초 실행하고 최초 source/test/log를 보존하며 관련 실패 target만 D17최대3회/
D18누적60분 안에서 다룬다. U01/L01/controller/R03/R04/R15의 기존 PASS는 반복하지 않는다.
완료선은 새6 methods와 subcase의 FAIL/ERROR/SKIP0, 선정2개 변이의 의미FAIL1/ERROR0,
기존 source/state 불변과 독립 최종 review다. 서명·실제 daemon·native 격리·실제 두 작업은 NOT_RUN이다.
U01b transport+durable, root/user PAUSE/operator gate, quota/control 불명을 해결하지 않고
후보를 실행 권한으로 바꾸지 않는다. 추가 사용자 승인이 필요한 새 범위를 만들지 않고 이미
허용된 개발·검사·독립 검토 절차로 진행한다.
