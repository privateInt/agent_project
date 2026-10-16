# U01a admission 소스 독립 정적 검토

판정: **REQUEST_CHANGES — 고정 reason 판정 순서에 관한 P2 finding 1건**.
소스·요구사항의 정적 검토이며 최종 실행 PASS가 아니다. 대기 중인 검사 파일·결과를
조회하거나 polling/wait하지 않았다.

## 검토한 정확한 pin

| 대상 | 고정 값 |
|---|---|
| 소스 | `/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/admission-development/src/ops/tools/codex_unattended_admission.py` |
| 소스 SHA-256 | `0bbc6cb46efcef3af84d4ffeca7eeccf94af3618073cdcba413254a2fd1ef91d` |
| 별도 개발 브랜치 — 읽기 조회로 확인 | `codex/agent-project-unattended-admission-20261016` |
| 개발 HEAD / PR15 기준 — 읽기 조회로 확인 | `c862bfc6951491192a9fe98b535c9a8be921ee35` |
| `/tmp/agent-unattended-admission-plan.md` | `321c5c9f8fa01246aeb627677505bc5581cc390174b47279b10f4760d489f410` |
| 사전 PASS `/tmp/agent-unattended-admission-plan-review.md` | `60483649f5ae98246455425785f4bfb4e1442209cdadc2b7e9b44cac9e038431` |
| 재사용 순수 함수의 원문 `/opt/codex-agent/scheduler-v2/flex_gate.py` | `30419990ee0cd77dfe37923ac6fe051b6b47b94469a39cdf00452291b5daf16b` |

위 소스 304행 전체, 승인 계획·사전 PASS와 재사용 함수의 고정 원문을 텍스트로 읽었다.
flex_gate는 import·호출하지 않았다. 소스의 schedule dependency 사용을 검토하기 위한
읽기였으며 서비스 상태·PAUSE 파일·운영 config·jobs/runtime은 조회하지 않았다.

## F1 · P2 · WINDOW_TOO_SHORT가 quota·settings 판정보다 뒤에 있음

위치: 소스 **269–283행**, **284–288행**. 기준: 승인 계획 **148–154행**.

계획의 고정 판정 순서는 schedule/window cap/lifetime cap 다음 잔여 시간 부족을 검사하고,
그 다음 usage·control·settings를 검사한다. 구현은 lifetime cap 직후 quota(269–271행),
control·settings(272–283행)를 검사하며, 후보 task의 잔여 시간 검사는 287–288행에 있다.

구체적 정적 경로:

- 올바른 T1 입력, admissions=[], 모든 pause=false, fresh snapshot, 유효한 현재 창과
  충분한 window cap, T1 timeout_sec=30을 전제로 한다.
- now_ms를 창 종료 149999ms 전으로 두면 필요한 150000ms보다 1ms 부족하다.
- 같은 입력의 ordinaryUsageAllowed=false이면 계획상 reason은 WINDOW_TOO_SHORT다.
  현재 소스는 270행에서 먼저 거부되어 USAGE_UNVERIFIED를 반환하고 288행에 도달하지 않는다.
- quota가 정상이고 settings가 불일치하는 조합도 WINDOW_TOO_SHORT보다
  SETTINGS_UNVERIFIED가 먼저 반환된다.

후보는 차단되지만 승인된 exact reason 우선순위와 다르다. 위 예시는 소스의 분기 순서로
도출한 것이며 테스트 실행·관찰 결과가 아니다.

최소 조치: 미배정 task가 있는 경우의 잔여 시간 검사를 계획에 정한 quota/control/settings
앞으로 배치하되, 후보가 없는 경우의 NO_PENDING_TASK 판정 순서는 함께 보존해야 한다.
필요한 확인은 기존 `test_schedule_quota_and_settings`의 관련 하위 사례 범위다.
새 method·추가 mutant·기존 통과 검사 반복을 요구하지 않는다. reviewer는 수정하지 않았다.

## 그 밖에 정적으로 확인한 계약

| 요구사항 | 확인한 구현과 범위 |
|---|---|
| 입력·strict type | `_plain`은 builtin JSON 형태·finite float·순환 참조를 검사한다. `_keys`, `_integer`, `_identifier`, `_hash`, `_path`, `_unique_list`가 exact keys·bool/int 구분·경로·해시·중복을 검사한다. 입력 오류는 고정 결과로 변환한다. |
| project/binding | agent_project literal, repository 형식, snapshot의 root/common-dir/base 일치, batch/선언 task/plan 결합, admission 순서와 predecessor head→다음 base를 검사한다. 첫 후보는 최초 base, T2는 완료한 T1의 head와 현재 snapshot head가 같아야 한다. config pin 자체의 신뢰는 외부 controller의 책임이다. |
| PAUSE | root→user→state 순서로 true 또는 None에서 차단한다. freshness 전에 검사하므로 stale true pause도 유지된다. dev unpaused로 root/user pause를 덮는 경로는 보이지 않는다. |
| 시간·일정 | 외부 now_ms·clock_floor·5개 관측의 age·미래 admission을 검사한다. max age 경계는 inclusive다. 고정 windows 함수는 가장 최근 시작 창을 먼저 반환하며 시작 inclusive/종료 exclusive, 자정·date override·겹침 계산을 재사용한다. 잔여 시간의 수치 조건은 timeout+120초 이상이다. F1은 그 조건의 위치에 관한 finding이다. |
| 상태·cap | UNKNOWN 네 종류는 후보를 막고 occupied 1개는 ACTIVE_TASK, 2개 이상은 STATE_CONFLICT다. window count는 admitted_at_ms를 사용한다. lifetime은 status와 무관한 len(admissions)이며 2부터 차단한다. 실패/불명 admission을 삭제하거나 환급하는 코드는 없다. |
| usage/control/settings | ordinaryUsageAllowed is True와 exhausted is False, control.available is True, desired_settings_supported is True를 요구한다. cwd·runtime roots·writable roots·workspaceWrite·network false·reviewer·on-request 또는 exact all-false granular를 검사한다. 결과의 auto_review/granular는 미적용 제안이다. |
| T1→T2 근거 | predecessor COMPLETED 후 같은 project/repository/batch/task/root/common-dir/base/head/plan의 canonical binding SHA를 검사한다. terminal, exact 필수 검사 ID/PASS와 strict zero 집계, 독립 review PASS와 서로 다른 actor ID, 같은 repo Draft PR/base/head/files 및 readback PASS/head/files가 모두 필요하다. 서명·역할의 실제 독립성·출처 인증을 주장하지 않는다. |
| 순수/noIO·출력 | imports는 datetime/hashlib/json/math/re뿐이다. 함수 내부 file/clock/socket/process/queue/settings IO, print, 동적 import 경로는 없다. schedule의 신뢰한 두 callable만 호출한다. 읽은 고정 validate_config/windows 및 그 순수 helper는 입력을 수정하지 않는다. 소스는 입력에 대입하지 않고 새 answer/candidate/proposal을 만든다. runtime_io_performed와 mutation_authorized는 항상 false다. |

이 표는 정적 확인이며 fixture 실행 결과나 native 환경의 권한 강제 증명이 아니다.
F1 외에 이번 고정 소스에서 구체적인 추가 요구사항 위반은 발견하지 못했다.

## 보존 및 미검증 경계

최종 판정에는 F1의 좁은 보정과 승인된 6 methods·2 mutants의 별도 실행 근거, 변경 pin의
독립 검토가 남는다. 이번 reviewer는 검사·probe·source 실행을 하지 않았고 결과를 추정하지
않았다. 실제 daemon·queue/turn·설정 적용·native 두 작업은 검토 범위 밖이다.

이번 쓰기는 이 리뷰 파일뿐이다. 제품·검사·계획·원본11개 로컬 변경·기존 결과·최초 오류는
수정하지 않았다. transport 재검토·검사 reconciliation도 수행하지 않았다.
root/user PAUSE·vms 제외와 D18 native 조사 보류를 유지했으며, socket/daemon/credential 접근,
권한 조사·위임·통지는 수행하지 않았다.
