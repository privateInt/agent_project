# Required-check snapshot auditor 사전 검사 계획

계획 버전: 1 / 2026-10-14. 기준 부모 `b1fda4916041fde8438251b04db62e6ff702c24f`, branch `codex/github-check-audit-20261014`. 새 격리 worktree의 시작 clean을 확인했으며 F0a worktree는 수정하지 않는다. 이 계획의 독립 사전 검토 전에는 소스/검사 구현을 시작하지 않는다.

## 기준과 작은 완료선

[GITHUB_GATES.md](common/GITHUB_GATES.md)의 필수 context·App ID·신뢰 경계와 [PREPARATION_TEST_PLAN.md](common/PREPARATION_TEST_PLAN.md)의 G03/G05/G06, [TESTING.md](common/TESTING.md)의 D29/D38을 참조한다. 공통 규칙·실패 기준·기존 운영 보류는 변경하지 않는다.

이번 산출물은 Python 3.10 stdlib 전용 순수 함수 `audit_check_runs(expected_head_sha, expected_apps, snapshot)`와 제한된 stdin CLI다. 입력에서 주장하는 App ID/head/검사 상태의 구조적 일치를 평가한다. 네트워크·credential·환경·파일 조회·subprocess·emitter·관리 setter·workflow·병합·worker는 구현하지 않는다. root가 소스를, test_agent가 검사/근거를, reviewer가 사전·최종 독립 검토를 담당한다. 변경은 소스·검사·이 계획·소형 offline 근거의 총 4파일로 한정한다.

**형식이 완전한 위조 snapshot은 정상 snapshot과 구별할 수 없다.** expected App ID도 호출자가 제공한 비교값이지 인증된 발행기 신원이라는 증거가 아니다. 이번 도구는 G03의 일부 비교 규칙을 준비할 뿐 G04의 독립성, G05 전체 결합, G06 trusted 실행·출처를 검증하지 않는다.

## 확정 입출력 계약

| 대상 | 필수 계약 |
|---|---|
| context | `codex/required-tests`, `codex/independent-review`, `codex/policy` 세 이름 고정. snapshot의 이름 목록이나 PASS 표시로 대체하지 않음 |
| expected 값 | head는 canonical 40자리 소문자 hex SHA. expected_apps는 고정 context 세 키에 대한 양의 `int`만 수용하며 bool/0/-1/float/string/null·누락/추가 키를 거부. 같은 양의 App ID가 여러 context를 담당하는 것은 허용 |
| snapshot | 객체의 `check_runs`는 list, `total_count`는 bool이 아닌 비음수 정수이며 정확히 list 길이와 같아야 함. `MAX_RUNS=1000`. 모든 row의 양의 정수 run `id` 중복을 거부하고 필수 context는 어느 App/head의 row든 동명 중복을 거부. 시각/가장 큰 id/배열 순서로 임의 선택하지 않음 |
| 필수 row | 정확한 name, expected `app.id`, expected `head_sha`, `status=completed`, `conclusion=success`만 구조 충족. 누락/불명확/실패·취소·skip·neutral·stale·진행 중·unknown은 BLOCKED. 비필수 row는 필수 검사를 대신하지 못하고 그 검사의 실패가 필수 context 판정에 섞이지 않음 |
| JSON 경계 | CLI stdin 단일 JSON은 정확히 `{expected_head_sha, expected_apps, snapshot}`. 읽기는 1MiB+1byte까지만 수행하고 1MiB 초과 거부. 중복 키·NaN/Infinity·손상 UTF-8/JSON·20자 초과 정수 token·중첩 초과 RecursionError를 고정 INPUT_INVALID로 처리. unknown snapshot/row field는 무시하되 출력/신뢰 근거로 사용하지 않음 |
| 공개 결과 | `scope=OFFLINE_ONLY`, status는 `STRUCTURAL_MATCH` 또는 `BLOCKED`뿐. structural_satisfied는 MATCH에만 true. 모든 결과에서 **input_origin_verified=false, trusted_gate_verified=false, binding_verified=false, native_ready=false, merge_authorized=false**. literal PASS나 실제 gate 성공 판정 없음 |
| 출력·부수효과 | 고정 허용 reason과 고정 context만 출력하며 입력 경로/SHA/App·run ID/raw field/private·예외 원문을 반환하지 않음. stdout은 허용 JSON, stderr의 원문/traceback 0건. 함수는 입력 불변·부수효과 없음. CLI도 구조 일치/불일치/입력 오류 모두 **exit 2**로 끝내 CI 성공으로 재사용되지 않게 함 |
| 실행 시간 해석 | CLI 자체의 wall-clock 제한·상시 stdin 감시는 구현하지 않음. 실행 예시는 완성된 입력 파일의 리다이렉션과 외부 `timeout 5s`를 사용. byte 상한을 시간 상한으로 주장하지 않음 |

기본 예상 경로는 `src/ops/tools/github_check_audit.py`, `src/ops/tools/tests/test_github_check_audit.py`, 이 계획, `docs/testing/github-check-audit-offline-20261014.json`이다. 구현 전에 root가 경로를 확정하고 다르면 동등 범위로 계획에 반영한다. source/test 구현과 검사 수는 아래 약 28개의 좁은 검사 주제에 맞추며 같은 검증을 중복 실행하지 않는다.

## 필수 검사 선정

| ID | 상황·입력 | 통과 기준 |
|---|---|---|
| C01 | 정상 세 필수 context·외부 비교 pin 2세트·공유 App ID | 라이브러리의 STRUCTURAL_MATCH/structural_satisfied=true, 모든 신뢰 불변값 false. 실제 GitHub App/commit 값을 하드코딩하지 않음 |
| C02 | 입력 순서 변경·유효한 비필수 검사 실패/진행 중 추가 | 필수 context의 판정 동일. 비필수 검사가 필수 성공을 대신하거나 불필요하게 섞이지 않음 |
| C03 | expected head의 누락/타입·길이·대소문자·공백 오류 | BLOCKED, 자동 보정이나 snapshot head로 대체 없음 |
| C04 | expected_apps 누락/추가/잘못된 키·비객체 | BLOCKED, 임의 App 허용이나 입력의 신뢰 claim 채택 없음 |
| C05 | App ID bool/0/-1/float/string/null | BLOCKED, type 혼동이나 숫자 강제 변환 없음 |
| C06 | snapshot/envelope/list/row의 잘못된 형식·필드 누락 | BLOCKED, 원문 비노출 |
| C07 | total_count의 bool/음수/float/불일치 | BLOCKED. 수가 맞는다는 사실도 수집 출처의 진위/완전성 검증으로 승격하지 않음 |
| C08 | empty/1000/1001 rows | empty는 필수 누락으로 BLOCKED, 유효한 1000행은 수용 가능, 1001행 거부 |
| C09 | run id 누락/bool/0/음수/float/string | BLOCKED, row를 조용히 버리거나 재번호하지 않음 |
| C10 | 서로 다른 이름에도 같은 run id 사용 | BLOCKED, 양의 run id의 전체 중복 거부 |
| C11 | 필수 context 하나 또는 전부 누락 | BLOCKED/structural_satisfied=false |
| C12 | 같은 App/head의 필수 context 중복 | BLOCKED, 최신/가장 큰 id/먼저 온 row 선택 없음 |
| C13 | 다른 App 또는 head의 동명 row와 정상 row 공존 | BLOCKED, 정상 하나만 골라 통과시키지 않음 |
| C14 | 필수 context의 다른 App ID | BLOCKED, context 이름만으로 수용하지 않음; D38-M1 대상 |
| C15 | 필수 row의 app/id 누락·잘못된 타입 | BLOCKED, slug/name이나 임의 기본값으로 대체 없음 |
| C16 | stale head·필수 row 간 head 혼합 | BLOCKED, timestamp 최신 주장이나 App ID 일치로 덮지 않음 |
| C17 | 필수 head 필드 누락·형식/타입 오류 | BLOCKED, expected head를 row에 주입하지 않음 |
| C18 | queued/in_progress/unknown/null status | BLOCKED, completion을 추측하지 않음 |
| C19 | failure/cancelled/timed_out/action_required/skipped/neutral/stale/unknown/null conclusion | success 이외는 전부 BLOCKED, skip/neutral도 성공으로 계산하지 않음 |
| C20 | context 대소문자·공백·부분 문자열·다른 이름 | 필수 이름으로 인정하지 않음 |
| C21 | 정확한 App ID까지 복사한 완전 위조·author PASS 주장 | 구조 일치는 가능하나 다섯 신뢰 불변값은 모두 false. 도구가 위조를 인증해 냈다고 주장하지 않음; D38-M2 대상 |
| C22 | 동일 head에 repo/PR/base/rules/plan/evidence 변경 주장 추가 | binding_verified=false 유지, G05 전체 결합을 검증했다고 보고하지 않음 |
| C23 | unknown field·URL·입력 경로·ID/SHA/private/예외 sentinel | 정상/오류 공개 결과와 CLI stdout/stderr에 비공개 값 0건, 허용 key/reason/context만 존재 |
| C24 | 정상/오류 함수 호출의 전후 입력·호출 순서 | 입력 deep-copy와 동일, 반복/순서에 따른 숨은 상태 변화 없음 |
| C25 | CLI 정상 구조 입력 | 구조 결과는 MATCH여도 exit 2, OFFLINE_ONLY와 다섯 false를 유지. 라이브러리 positive와 실제 CI 성공을 구분 |
| C26 | CLI 구조 불일치/입력 오류/위조 입력 | exit 2·고정 허용 JSON, 원문 stderr/traceback·입력 echo 없음 |
| C27 | CLI 중복 키·NaN/Infinity·손상 UTF-8/JSON·큰 정수·중첩·1MiB 경계 | 고정 INPUT_INVALID. 유효 JSON에 공백을 더한 정확한 1MiB는 처리 가능하고 1MiB+1은 거부. 20자 정수 token 상한과 read cap을 확인 |
| C28 | stdlib·Python3.10·부수효과·문서/diff | AST3.10과 실제 이용 가능한 Python 실행을 구분. 도구의 network/credential/환경·파일 조회/subprocess/write 0건, 계획 링크·변경 4파일·비밀 비노출·diff 검사 통과 |

## D38의 의미 오류 검출

reviewer가 다음 두 개를 선정한다. 정상 소스의 목표 검사가 통과하고 격리 복사본의 같은 검사가 의도한 의미 오류 때문에 assertion FAIL해야 한다. 문법/import/환경 실패는 인정하지 않는다.

| 변이 | 잘못 바꿀 로직 | 실패해야 하는 기대 결과 |
|---|---|---|
| M1 | 필수 context의 expected App ID 비교 제거 | C14의 다른 App row가 잘못 STRUCTURAL_MATCH가 되어 BLOCKED 기대 assertion 실패 |
| M2 | 구조 MATCH일 때 `trusted_gate_verified=true`로 승격 | C21의 정상처럼 보이는 위조 입력에서 고정 false 기대 assertion 실패. provenance를 입증하지 않고 gate로 승격한 오류를 검출 |

변이는 임시 복사본에서만 수행하고 원본 소스 불변·복사본 삭제·source/test/plan/mutant hash와 실패 원인을 소형 근거에 남긴다. 기존 준비 계획의 full G04/G05/G07 emitter 검증력 검사를 대신하거나 삭제하는 것이 아니다.

## 완료 판정과 미완료 범위

사전 검토 후 root가 소스를 구현하고 test_agent가 위 관련 검사만 수행한다. source/test/계획 hash·명령·실제 Python 버전·검사 수/시간·PASS/FAIL/ERROR/SKIP·D38 근거를 연결하고 독립 최종 검토를 받는다. Python 3.10 실제 실행 환경이 없으면 문법 검사와 별도로 미실행을 명시한다. 공유 코드 수정이 없으면 기존 F0a/정의조회/서빙·제품 전체 suite는 반복하지 않는다.

도구의 offline 구조 검사가 완료돼도 trusted App/발행기·출처·독립 reviewer·repo/PR/base/rules/plan/evidence 전체 결합·GitHub 보호 readback/실제 위조 거부·원자 병합은 계속 미구현/미인수다. 어떠한 결과도 native 준비나 병합 허가가 아니다. 기존 sandbox EACCES의 D17/D18 보류·누적/연장 소진은 이 독립 작업으로 초기화하거나 해제하지 않는다.
