# GitHub 증거 binding 필드 비교 사전 검사 계획

계획 버전: 1 / 2026-10-14. 기준 부모 `66b3f4fb1e054862e2b3a94e167982f0c5173a5f`, branch `codex/github-evidence-binding-20261014`. 새 worktree의 시작 clean만 확인했으며 기존 PR5 worktree는 수정하지 않는다. 이 계획의 독립 사전 검토 전에는 소스/검사 구현을 시작하지 않는다.

## 범위와 기준

[GITHUB_GATES.md](common/GITHUB_GATES.md)의 증거 결합·신뢰 경계, [PREPARATION_TEST_PLAN.md](common/PREPARATION_TEST_PLAN.md)의 G03/G05/G06, [기존 auditor 계획](github-check-audit-test-plan.md), [TESTING.md](common/TESTING.md)의 D29/D38을 따른다. root는 기존 `src/ops/tools/github_check_audit.py`에 두 순수 API만 추가한다: `canonical_binding_digest(binding) -> str | None`, `audit_bound_checks(expected_binding, expected_apps, evidence) -> dict`.

변경은 기존 소스·새 `src/ops/tools/tests/test_github_evidence_binding.py`·이 계획·`docs/testing/github-evidence-binding-offline-20261014.json`의 4파일이다. test_agent는 새 검사/근거, reviewer는 사전·최종 독립 검토를 담당한다. CLI 새 모드·network·credential·환경/파일 조회·subprocess·emitter·관리·merge·workflow·worker는 추가하지 않는다.

필드와 unkeyed digest의 비교는 출처 인증이 아니다. expected/observed binding과 snapshot/App pin을 양쪽에서 함께 위조하고 digest도 다시 계산하면 구조적으로 일치할 수 있다. 실제 최신 repo/base/head나 artifact bytes·독립 reviewer·trusted emitter를 검증했다고 주장하지 않는다.

## 확정 계약과 보존

| 대상 | 필수 계약 |
|---|---|
| binding shape | 정확히 7키: `repository`, `pr_number`, `base_sha`, `head_sha`, `release_sha256`, `plan_sha256`, `evidence_sha256`. expected/observed 양쪽 모두 엄격 검사. 잘못된 binding의 canonical API는 예외/원문 없이 None 반환 |
| 값 | repository는 정확히 하나의 `/`로 구분한 `[A-Za-z0-9_.-]{1,100}` 두 token이며 각 `.`/`..` 금지. 공백 제거·대소문자 보정 없음. pr_number는 bool이 아닌 int 1..2^63-1. base/head는 40자리 lower hex, 세 digest는 64자리 lower hex |
| canonical digest | 정확한 bytes prefix `b'agent-project:github-evidence-binding:v1\n'` 뒤에 `json.dumps(binding,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')`를 붙여 SHA-256 lower hex 반환. prefix는 newline 1byte를 포함하고 JSON 끝의 추가 newline은 없음. 입력 key 순서는 무관하며 유효한 각 필드 값 변화는 고정 fixture의 digest를 바꿔야 함 |
| evidence | 정확히 `{binding,binding_digest,snapshot}`. observed binding의 재계산 digest에 claimed canonical64 digest가 일치하고 expected binding digest에도 일치해야 함. invalid expected/observed의 None을 서로 같다고 수용하지 않음 |
| artifact 의미 | evidence_sha256는 별도 필수 실행 근거 artifact의 **주장된** hash다. 현재 evidence envelope/snapshot의 hash가 아니고 artifact bytes를 읽어 재계산하지 않음 |
| 기존 검사 결합 | binding 일치 후 기존 `audit_check_runs(expected_binding['head_sha'],expected_apps,snapshot)`에 위임. 기존 App/head/status/conclusion·중복/누락/limit 기준 유지. 두 binding과 claimed digest가 일치하면 binding_fields_satisfied=true이며, 뒤의 snapshot/head/App/검사 실패로 전체 BLOCKED여도 이 값은 true. binding 단계 실패면 false |
| 새 결과 | 기존 결과의 고정 10키+`binding_fields_satisfied` bool의 11키만 반환. `scope=OFFLINE_ONLY`, status=STRUCTURAL_MATCH/BLOCKED, structural_satisfied는 전체 판정. input_origin_verified/trusted_gate_verified/**binding_verified**/native_ready/merge_authorized는 모든 경우 false. report에 binding 값·digest·private/raw field·예외 원문 출력 없음. canonical API의 정의된 정상 반환 digest는 이 report와 구분 |
| 이전 계약 | 기존 함수 9개와 CLI entrypoint의 AST·본문 원문, 기존 constant 값을 기준 부모와 대조해 보존. 새 import는 hashlib만. 기존 audit_check_runs 결과 10키·stdin 계약·1MiB/정수20자 경계·항상 exit2·출력 I/O 비노출 유지. old test28 파일도 변경하지 않음 |

기준 소스 SHA-256은 `3a5f8b6a42b0fae6e93118e66accd8e5bd94eb2cbb3451f9db9fd72cd3c2c69e`, 기존 `test_github_check_audit.py` SHA-256은 `fcc0b85600114a578f55b8e075bc2519d4b71d900e5ddcd73faaf81b7e5539c5`다. 보존 대상 함수는 `_result`, `_positive_int`, `_canonical_sha`, `audit_check_runs`, `_unique_object`, `_bounded_integer`, `_finite_float`, `_reject_constant`, `main`이다. 전체 소스 hash는 새 API 추가로 바뀌므로 기존 함수/CLI 원문 보존과 구분한다.

## 독립 고정 digest vector

reference는 제품 함수를 호출하거나 제품의 json.dumps 경로를 기대값 계산에 재사용하지 않고 아래 명시적 ASCII bytes에 hashlib.sha256을 적용해 고정했다. prefix 41bytes+아래 JSON 413bytes다. 코드 검사에서는 다음 literal 기대 hash를 사용한다.

```json
{"base_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","evidence_sha256":"eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee","head_sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb","plan_sha256":"dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd","pr_number":17,"release_sha256":"cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc","repository":"ExampleOrg/example_repo"}
```

기대 SHA-256: `bda08a5181637f313a0a74ac01c8e15baf4189b2ab9e7b703dd1a719329ac931`.

## 필수 검사 선정

새 suite는 아래 18개 주제와 필요한 subtest로 한정한다. 실제 검사 수/선정 버전과 각 결과를 기록하며 같은 범위의 중복 검사를 만들지 않는다.

| ID | 상황/입력 | 기대 결과 |
|---|---|---|
| B01 | 고정 vector와 입력 key 순서 변경 | literal digest 정확히 일치, dict 순서만 바꾸면 같은 digest, 출력 lower64 |
| B02 | 유효 repository token 1/100자·허용문자·대소문자 차이 | 경계는 수용. 대소문자 차이는 서로 다른 binding 값으로 유지하고 digest 변화 |
| B03 | expected/observed 비객체·7키 누락/추가·잘못된 key | canonical=None, bound 결과 BLOCKED/structural=false/binding_fields=false, 다섯 신뢰 값 false |
| B04 | repo 빈값·slash 0/2개·token101자·`.`/`..`·공백/제어/Unicode/기타문자 | 보정 없이 거부, private 문자열 비노출 |
| B05 | pr 1/2^63-1 및 0/음수/overflow/bool/float/string/null | 유효 경계만 수용, 나머지 canonical=None |
| B06 | base/head40·digest64 필드의 길이·upper/비hex·타입 오류 | expected/observed 모두 동일 기준으로 거부 |
| B07 | 유효한 7필드 값을 각각 하나씩 변경 | 각 변경 digest가 원래 값과 다름. 입력 원본 불변 |
| B08 | 정상 binding+claimed digest+정상 snapshot | 결과 exact11키, structural=true/binding_fields=true, 다섯 신뢰 값 false |
| B09 | evidence 비객체·정확한 3키 누락/추가 | BLOCKED/binding_fields=false, raw/unknown field를 신뢰하거나 출력하지 않음 |
| B10 | 7필드별 expected/observed 불일치, claimed는 observed에 올바르게 재계산 | BLOCKED/binding_fields=false. M1은 **base만 변경**해 snapshot head 검사가 오류를 가리지 않게 함 |
| B11 | claimed digest 누락/타입·길이·upper/비hex 및 **유효한 다른 lower64 값** | BLOCKED/binding_fields=false. M2는 두 binding이 동일하고 claimed만 유효한 틀린 값인 사례 |
| B12 | invalid expected+invalid observed+claimed=None | None==None 경로로 승격하지 않고 BLOCKED/binding_fields=false |
| B13 | bindings/claimed 정상, 뒤의 App/status/conclusion/누락 등 기존 check 실패 | 전체 structural=false/BLOCKED이면서 binding_fields=true. 다섯 신뢰 값 false |
| B14 | binding head 정상 일치, snapshot의 required head가 다름 | 기존 head 검사로 BLOCKED, binding_fields=true. 필드 일치와 전체 검사 결과를 혼동하지 않음 |
| B15 | 임의 유효 artifact 주장 hash와 snapshot unknown metadata 변화 | artifact bytes 검증/현재 snapshot hash로 오인하지 않음. snapshot의 비검사 metadata 변화만으로 binding digest가 바뀌지 않으며 신뢰 값 false 유지 |
| B16 | 양쪽 7필드·App/snapshot을 함께 위조하고 claimed digest 재계산 | 구조 MATCH와 binding_fields=true는 가능하지만 모든 신뢰 값 false. 인증/live/fullG05로 해석하지 않음 |
| B17 | 정상/오류 private repo/SHA/digest/경로/미지값, 반복 호출 | exact report allowlist·고정 reason/context, 원문 stdout/stderr0·입력 불변·숨은 상태 없음 |
| B18 | 기존 계약 보존·기존28+새 suite·stdlib/Python3.10·문서/diff | 원문/old test hash 대조와 회귀 통과, API 순수성·hashlib 외 새 import 없음. AST3.10과 실제3.10 실행을 구분하고 불가하면 미실행 명시 |

## D38 선정

reviewer에게 아래 두 의미 오류를 선정받는다. 정상 목표 검사는 PASS, 격리 복사본의 같은 검사는 의도한 구조 오판 때문에 assertion FAIL·ERROR0이어야 한다.

| 변이 | 주입 오류 | 목표 검출 |
|---|---|---|
| M1 | `if expected_digest != observed_digest:` 비교/차단 제거 | B10에서 base만 다른 관찰값과 올바른 observed claimed digest가 잘못 수용돼 BLOCKED/false 기대 assertion 실패 |
| M2 | `if claimed_digest != observed_digest:` 비교/차단 제거 | B11에서 정상 동등 binding과 유효한 틀린 claimed digest가 잘못 수용돼 BLOCKED/false 기대 assertion 실패 |

각 target에서는 다른 validation이 변이를 대신 차단하지 않게 유효한 입력을 사용한다. 원본 source/test hash 전후 일치, mutant hash, 정상/변이 목표 결과와 원인, 임시 복사본 삭제를 기록한다. syntax/import/환경 실패는 검출 성공으로 세지 않는다. 기존 auditor의 App guard/신뢰 승격 D38 근거는 보존하고 다시 실행하지 않는다.

## 완료선과 남는 한계

source 변경의 영향으로 기존28 회귀 1회와 새 좁은 suite를 실행하고 새 M1/M2만 검증한다. 최종 source/test/계획의 실행 전후 hash, 실제 Python/명령/검사 수/시간·PASS/FAIL/ERROR/SKIP와 독립 최종 검토를 연결한다. 변경/새 실패/미해결 우려가 없으면 더 넓히거나 반복하지 않는다. native GitHub/credential/emitter/관리/merge/모델·sandbox 실행은 0건이다.

종료 판정은 **offline binding 필드·claimed digest 대조의 격리 검증**이다. 인증된 fetch·expected pin의 진위·artifact bytes·최신 base/head·full G05·G06·실제 gate/병합 인수는 미완료다. 기존 CLI는 binding 모드를 제공하지 않으며 어떤 CLI 결과도 exit0/실제 CI gate 성공이 아니다. 기존 sandbox EACCES의 보류·누적·연장 소진과 기타 프로젝트 실패 이력은 이 새 단위로 초기화하거나 해제하지 않는다.
