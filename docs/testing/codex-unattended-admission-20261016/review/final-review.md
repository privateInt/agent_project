# U01a 순수 admission 최종 독립 검토

판정: **PASS — 고정 U01a 소스·검사·fixture와 범위별 합성 실행 근거에 한정**.
기존 P2 잔여 시간 reason 우선순위 finding은 해소됐으며 미해소 finding은 발견하지 못했다.
후보는 실행·설정 변경 권한을 부여하지 않는다. runtime/control/native 작업의 PASS가 아니다.

## 정확한 검토 pin

개발 경로: 원 repo의 `var/unattended-20261015/admission-development`.
읽기 조회로 브랜치 `codex/agent-project-unattended-admission-20261016`와
HEAD/PR15 기준 `c862bfc6951491192a9fe98b535c9a8be921ee35`를 확인했다.
변경 목록은 U01a 소스·검사·fixture 2개·관찰 JSON·문서 4개다. transport는 검토 대상이 아니다.

| 대상 — 개발 경로 기준 | SHA-256 |
|---|---|
| `src/ops/tools/codex_unattended_admission.py` | `b012469262a0de2b9025b393ea2dadc6799933eb1a57fe10c68187809d85da51` |
| `src/ops/tools/tests/test_codex_unattended_admission.py` | `33d6ac1d164c5fa5e6c11b8e4dbce062cb9a65735e4bd95158a2f28c6af1d84b` |
| `src/ops/tools/tests/fixtures/codex_unattended_admission/flex_gate.py.txt` | `30419990ee0cd77dfe37923ac6fe051b6b47b94469a39cdf00452291b5daf16b` |
| 같은 fixture 디렉터리의 `synthetic.json` | `2dbd92639c906232ac9492c2f200fb03ba082c7fc2308ee25472f75943c4aacc` |
| `docs/testing/codex-unattended-admission-observed-20261016.json` — review PENDING인 검토 시점 원문 | `88a127b096a46bc5225be086859cda56016e71e357b82019048ede85cc9da4d5` |

| 검토 기준 | SHA-256 |
|---|---|
| `/tmp/agent-unattended-admission-plan.md` | `321c5c9f8fa01246aeb627677505bc5581cc390174b47279b10f4760d489f410` |
| 사전 PASS `/tmp/agent-unattended-admission-plan-review.md` | `60483649f5ae98246455425785f4bfb4e1442209cdadc2b7e9b44cac9e038431` |
| 최초 P2 `/tmp/agent-unattended-admission-static-review.md` | `2b1af94ae3f13ff931f21a861d6b0350ad92cd475f204a12700ea3efc0978df2` |

위 pin의 파일 텍스트·diff·실행 근거를 읽었으며 제품·검사·gate를 import하거나 실행하지 않았다.

## 코드와 P2 수정

소스 305행·최종 검사 437행·gate fixture 159행·합성 JSON 전체와 실제 diff를 읽었다.
현재 source의 이동한 블록과 빈 task 처리만 원래 위치·형태로 되돌린 **메모리상의 텍스트**는
기존 `0bbc6cb46efcef3af84d4ffeca7eeccf94af3618073cdcba413254a2fd1ef91d` 해시와 정확히 같았다.
따라서 0bbc→b012의 차이는 pending task의 WINDOW_TOO_SHORT 검사를 quota/control/settings
앞으로 옮기고, task가 없을 때의 기존 NO_PENDING_TASK 순서를 보존한 최소 보정이다.
다른 guard나 완료 기준 변경은 없다. 파일을 되돌리거나 소스를 실행한 것은 아니다.

`margin-fix-audit.json`과 최초 실행 pin은 보정이 **첫 6개 검사 전에** 이뤄졌음을 구분한다.
이 finding에 대한 실행 FAIL-before가 있었다고 주장하지 않는다. 최초 정상 검사에는 잔여
시간 ±1ms·정확 경계와 quota=false/unsupported 설정이 동시에 있는 우선순위 사례가 포함돼
WINDOW_TOO_SHORT를 기대한대로 확인했다.

| 요구사항 | 정적으로 확인한 구현 |
|---|---|
| 입력·결합 | builtin JSON 형태·finite float·순환·exact keys·strict int/bool·경로·해시·중복을 검사한다. agent_project, repository/root/common-dir/base snapshot 결합과 batch/task/plan/admission 순서·predecessor head→base를 검사한다. config의 실제 신뢰 출처는 외부 책임이다. |
| PAUSE·시간 | root→user→state에서 true/None을 차단하고 freshness보다 먼저 처리한다. 외부 now_ms와 clock floor, 5개 observation의 age·미래 timestamp 및 미래 admission을 검사한다. freshness 상한은 inclusive다. |
| 일정·cap | 두 고정 순수 함수로 자정·date override·최근 시작 창을 재사용한다. admitted_at_ms로 window count를 세고 timeout+120초 여유를 요구한다. occupied 1개/여러 개와 UNKNOWN 네 종류를 차단하며 status와 무관한 lifetime 2를 유지한다. |
| usage·settings | ordinaryUsageAllowed is True, exhausted is False, control과 desired settings 지원을 요구한다. cwd/roots/workspaceWrite/network false와 reviewer/policy를 검사하며 all-false granular·auto_review는 제안만 반환한다. |
| T1→T2 | predecessor COMPLETED와 같은 project/repository/batch/task/root/common-dir/base/head/plan의 canonical binding SHA를 요구한다. terminal·정확한 필수 검사 PASS/strict zero 집계·독립 review PASS/서로 다른 actor·같은 repository의 Draft PR·head/files readback 전체가 결합돼야 한다. 현재 head도 게시 head와 같아야 한다. |
| 순수성 | 제품은 file/clock/socket/process/queue/settings IO나 동적 import·print를 하지 않는다. 입력을 변경하지 않고 새 출력만 구성하며 runtime_io_performed/mutation_authorized는 항상 false다. 실제 두 callable과 순수 helper는 계산만 한다. |

검사의 selection wrapper는 입력 deep-copy 대조, 출력 exact keys/제안 비교와 IO·clock·import
거부를 적용한다. 거부 예외를 BaseException으로 두어 제품의 일반 Exception 처리로 숨겨지는
시도도 성공으로 오인하지 않는다. 검토한 코드·검사에 새로운 우회 동작은 없다.

## 실제 실행 근거와 정확히 두 변이

아래 경로는 원 repo의 `var/unattended-20261015/transition-evidence/` 기준이다.
각 로그·marker·runner·manifest·result와 mutation/source diff 및 portability audit를 읽었다.
실행자는 test_agent이며 reviewer는 재실행하지 않았다.

| 단계 | 실제 결과 | result SHA-256 |
|---|---|---|
| `admission-run-1/normal-result.json` | sourceb012/test8c525의 최초 6 methods PASS, FAIL/ERROR/SKIP 0. gate setup load 1. | `d58f8e139242c6566410d108f68e7ad049af86154b74daed7a47b07664951d79` |
| `admission-run-1/mutants/M1/result.json` | 지정 단일 target `test_lifetime_cap_blocks_third_candidate`만 실행, semantic FAIL1/ERROR0/SKIP0. | `fde3b9f5a8729520b5269ca0b7147c9e38f4e23dd194951fb940917bbcc6ecbf` |
| `admission-run-1/mutants/M2/result.json` | 지정 단일 target `test_t2_pending_review_blocks`만 실행, semantic FAIL1/ERROR0/SKIP0. | `45c3c08cff6104776474707927dafb48ee95bb248ebb0664c2870d7851b209f9` |
| `admission-portability/result.json` | 새 helper `case_portable_t1_t2_candidates` 1개 PASS, FAIL/ERROR/SKIP 0. fresh renamed source tree에서 T1·full-evidence T2 두 선택 관찰. | `2c3d0c4f47162056a51b3a8bb240b70ee961f2e087bbc856af750d35954f86a2` |

최초 test pin은 `8c525d67d236627227080b51f4605afc8b9a69daeec7c47edb2276a5ff4effc1`이고,
최초 통합 결과 `admission-run-1/result.json`의 SHA는
`91253fabc1bdd89934585c8b46e18743fea67e22b5d8a27c46b8f51250a8e601`이다.

M1은 lifetime guard 한 줄만 생략했다. 다른 조건이 모두 유효한 T3가 CANDIDATE_READY로
선택돼 LIFETIME_CAP_REACHED assertion에서 실패했다. mutant SHA는
`70f919a4abeae6791b6f3d8330c772bb18f0fb4b948813b174e5e60126fc0f15`다.
M2는 review/readback 완료 guard 한 줄만 생략했다. review=PENDING인 T2가 선택돼
EVIDENCE_UNVERIFIED assertion에서 실패했다. mutant SHA는
`3e6c79ec22152354978c5cb148f2444c8216f5c21ae349b2efabeef736bb5aa1`다.
두 실패 모두 다른 guard·fixture ERROR가 원인이 아니며 실제 dispatch는 0이다.

runner는 정상 6개 PASS 후에만 두 변이를 만들고 각 지정 target을 한 번만 실행했다.
각 target의 정상 PASS는 최초 결과에서 재사용했고 재실행하지 않았다. 같은 초기 프로세스의
gate load는 정상+변이 전체에서 1이다. 원래 제품·정상 결과·최초 오류는 보존됐다.
해시 목록의 초기24개+portability18개, 총42개 파일도 실제 bytes와 모두 일치했다.

## 변경 범위 재사용과 /opt 의존성 제거

최종 test diff는 GATE_PATH를 인접 fixture로 변경하고, 새 helper와 첫 method의 helper 호출
한 줄을 추가한 것이다. reviewer의 AST 대조에서도 이 세 변화만 되돌리면 최초 test와 전체
AST가 같았다. 첫 method의 그 호출을 제외한 본문·assertion 및 다른 모든 기존 함수의 source
bytes는 유지됐다. method 수는 6이며 두 mutant target도 그대로다.

제품은 최초 검사 이후 같은b012다. 기존 정상·변이 근거는 변하지 않은 제품/입력/guard에
재사용하고, 바뀐 dependency 로딩 경로는 새 helper에서 정상 T1과 모든 완료 bundle을 가진
T2로 확인했다. **최종33d6 test의 전체 6 methods와 두 변이는 다시 실행하지 않았다.**
이 사실을 최종 모듈 전체 실행 PASS로 바꾸거나 불필요한 반복을 요구하지 않는다.

초기 검사는 설치 원문의 고정30419990을 한 번 로드했다. 현재 fixture는 그 보존 원문과
동일 bytes인 검사용 참조이며 새 runtime framework fork가 아니다. 최종 setup은 fixture
해시를 먼저 검사하고 validate_config/windows만 주입한다. default_config/decide/read_config/
safe_json_file/main은 거부 함수로 대체한다. fixture 안의 /etc·운영 경로 문자열을 실제로
읽는 entrypoint는 호출하지 않는다. 제품 자체에는 /opt 경로 의존이 없다.

portability runner는 새 source tree와 /usr 라이브러리만 읽을 수 있는 guard 아래에서
import·변하지 않은 setup·새 helper를 수행했다. 원래 /opt/private checkout 읽기 시도·실제
쓰기/socket/process 시도는 0, 복제된 gate 읽기는 1이었다. 이 **별도 fresh 프로세스**의
gate load 1은 초기 정상+변이 프로세스의 1과 별도 근거다. 초기 카운터를 덮거나 같은 실행의
재로딩으로 합산하지 않는다. 이번 reviewer는 설치 gate나 실제 운영 상태를 다시 조회하지 않았다.

## 문서·요약과 합성 범위

네 문서의 실제 diff와 U01a/보류 관련 본문은 합성 검사·변이·이식성과 실제 queue 실행을
구분하고 있다. 검토 시점의 문서 pin은 아래와 같다.

| 문서 | SHA-256 |
|---|---|
| `docs/HANDOFF_NEXT_SESSION.md` | `fabb57bf49e262801cff9e24ce45db1bd879ea52694623c813528369fef34a65` |
| `docs/development.md` | `7b7e3c08f59b0a8b697c15c31a5f2704deb72c14fed1278f660089517fb93cc4` |
| `docs/roadmap.md` | `fa262c1b6c45dce38486fe5012ee85ad4b36ae1f346dfe04fc2b9b046723859b` |
| `docs/status.md` | `1cceca2fe4dd2585588593fab0a633bfe2c2967c7a28ee066ae29c639508c82e` |

요약 JSON의 source/test/fixture/단계별 result pin은 실제 파일과 일치한다.
final_independent_review=PENDING은 검토 전 상태이므로 결함이 아니다. 판정 뒤 review 정보만
갱신·복사할 수 있으며 새 요약 SHA를 기록해야 한다. 위88a127 pin은 검토 시점 원문이다.
제품·검사·fixture·실행 횟수·실행 범위를 바꾼 요약에 이 판정을 소급하지 않는다.

실제 수행된 것은 합성 입력에 대한 Python 검사·변이·이식성 검증이다. fixture-owner,
/fixture 경로, 고정 과거 시각·head/plan 값과 완료 bundle은 **SYNTHETIC_ONLY**다.
그 PASS가 실제 소유자 승인·검사 서명·독립 reviewer 출처·현재 quota/PAUSE 상태나 실제
repository/OS 경계의 인수는 아니다. candidate와 desired_settings는 비권위적 출력이다.

서명/provenance·owner 승인·보호된 controller와 submission journal/CAS/fsync·native 두
작업은 NOT_RUN 또는 이번 구현 범위 밖이다. 실제 Auto-review 적용·worker/scheduler 활성화도
인수하지 않는다. D18 held·root/user PAUSE·vms 제외와 운영 원본/DB/Docker/credential 보호를
유지한다. transport PR16의 별도 게시·readback은 이번에 반복하지 않았다.

다음은 coordinator의 U01a 전용 commit/Draft PR·근거와 이 검토의 복사·게시 bytes readback이다.
runtime/control/권한 조사나 실제 실행 승인 단계가 아니다. 이번 reviewer의 쓰기는 이 검토
파일 하나뿐이며 제품·검사·문서·계획·원본11개 변경·기존 결과·최초 오류를 수정하지 않았다.
검사 실행·다른 agent polling/wait·socket/credential 접근·통지·위임도 수행하지 않았다.
