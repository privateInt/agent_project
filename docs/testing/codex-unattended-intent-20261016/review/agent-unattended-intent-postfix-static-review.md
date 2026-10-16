# U01b1 수정 후 소스·현재 검사 정적 감사

판정: **두 수정 확인. 현재 pin에서 추가 제품 finding 없음. 검사 보완·실제 실행·최종 판정은 PENDING.**
이는 실행 PASS가 아니다. 제품·검사·DB를 실행하지 않았고 결과 대기·polling도 하지 않았다.

| 읽은 대상 | SHA-256 |
|---|---|
| `intent-development/src/ops/tools/codex_unattended_intent_journal.py` — 306행 | `902fb39e9d792d7e4a022fdc6e0eb6fdff8089d823c079762b444fba070a32d8` |
| 같은 개발 사본 `src/ops/tools/tests/test_codex_unattended_intent_journal.py` — 읽은 시점545행 | `51ac9929e2bcc7b2ed46572f8e5015e531f600130147f5b14d35be205d971c67` |
| 같은 test 디렉터리 `fixtures/codex_unattended_intent_journal/synthetic.json` | `3932939c84c22a0875e5fa8141a70869738132f32b8b68f0bccce47a80074a66` |
| 보존 계획 `transition-evidence/agent-unattended-intent-plan-v1.md` | `4c005dd7c2a7703f0639ef71aed968c5a1fc5cf8b6ce404364debb779097d575` |
| `/tmp/agent-unattended-intent-plan-review.md` | `f1fb9dad4822a897f13239cc5fc5fdae4734b7000240a21f056f78767cd476ee` |

개발 사본과 transition-evidence는 원 repo의 `var/unattended-20261015/` 아래다.
소스·현재 검사·fixture 전체와 보존 snapshot의 실제 diff 및 두 수정 audit를 읽었다.

## 소스 수정 확인

**F1 preflight 분류:** `_connection` 132–137행의 try는 구조적 연결 조건만 다룬다.
SQL 조회 138–147행은 그 예외 축약 밖에 있다. PRAGMA/조회/cursor 예외나 재사용 `_one`의
불명 예외는 `_run`의 일반 예외 경로로 전달돼 CHECKPOINT_UNCONFIRMED/UNCERTAIN,
new_claim=false·record=None이 된다. 조회로 확인된 잘못된 busy/synchronous/journal/DB profile은
별도의 `_need(..., CONNECTION_INVALID)`로 BLOCKED를 유지한다. 기존 ledger의 원인 축약
helper를 재사용하던 경로는 제거했고 bounded SQL primitives는 그대로 사용한다.

**key 검사 순서:** 97–99행은 exact builtin tuple·길이 다음 모든 component의 exact builtin
str/identifier를 검사하고 마지막에 project equality를 수행한다. tuple subclass나 non-string
component를 검사하는 동안 caller의 임의 equality를 먼저 호출하는 기존 순서가 사라졌다.
read/claim의 malformed key는 SQL 진입 전에 ARGUMENTS_INVALID로 반환되는 정적 경로다.

보존된 원문은 c4c6f602→74f2799b→902fb39e 순이다. 실제 c4→최종 diff는 sqlite3 import와
위 `_key`/`_connection` 수정뿐이다. 원문·두 diff·audit를 보존했으며 실행 FAIL-before가
있었다고 주장하지 않는다. audit는 두 보정이 첫 검사 전에 이뤄졌다고 명시한다.

claim 295–299행의 실제 UPDATE에는 key+pin+expected revision+state=PREPARED가 모두 남아
있다. 동일 transaction의 0-row 분류, commit 확인 후 새 claim 보고, 실제 commit 뒤 불명과
소유 transaction 정리도 변경되지 않았다. M1 revision predicate/parameter 생략 및 M2 state
predicate 생략의 지정 범위는 그대로다. 초기화·불변 binding·correlation 충돌·UNKNOWN 재arm
금지·remote authority=false에도 이번 diff의 변경은 없다.

공용 ledger/collector SHA는 각각
`5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`,
`4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`로 유지된다.

## 현재 검사와 남은 delta

현재 파일은 계획의 5 methods를 유지한다. dedicated TemporaryDirectory/file DB, caller-owned
connection, schema/profile·원문·digest·correlation/충돌·close/reopen·실제 두 connection CAS·
commit 전후/rollback 실패 주입과 외부 IO 거부를 다룬다. fixture는 합성 project/task/prompt/
settings digest를 독립적으로 계산·대조하며 실제 owner/권한/원격 제출을 가장하지 않는다.

M1 `test_stale_revision_rejected`는 PREPARED/rev0에서 expected1만 다른 단일 사례다.
M2 `test_unknown_never_rearmed`는 정상 claim 후 재open한 UNKNOWN/rev1에 expected1을 전달한다.
둘은 각각 지정 guard 하나만 빠졌을 때 의미 실패하도록 구성돼 있고 추가 하위 사례는 없다.

검토한51ac/545행 버전에는 정상/부적합 profile 사례가 있지만 다음 보완은 **아직 없다**:

- preflight `_one`/`_query`의 실제 사용 경로에서 SQL·cursor 응답 예외를 주입해
  CHECKPOINT_UNCONFIRMED를 확인하는 하위 사례. 현재 실패 stage는 BEGIN/UPDATE/COMMIT/
  ROLLBACK부터다. 알려진 invalid profile의 CONNECTION_INVALID와 구분하는 검증이 남는다.
- non-builtin tuple/component 또는 hostile equality를 가진 key를 read/claim에 넣어
  equality/SQL 호출 없이 ARGUMENTS_INVALID로 거부함을 확인하는 하위 사례.

사용자가 예고한 다음 test delta로 구분하며 제품 수정 실패나 실행 회귀로 보고하지 않는다.
기존 비-target method에 이 사례를 추가하고 5 methods·두 isolated target을 유지하는 범위다.
계획·기대 reason·변이 범위를 바꾸거나 여섯 번째 method/추가 변이를 요구하지 않는다.

`transition-evidence/intent-run-1`은 한 번 확인한 시점에 없었다. 재조회하지 않았다.
따라서 최초5개·정확2개 변이·추가 하위 사례의 실제 결과는 이 검토에서 모두 **미검증/PENDING**이다.
이후 생성 여부를 추정하거나 최종 실행 PASS로 소급하지 않는다.

마지막 일관성 SHA 대조에서 제품은902fb39e 그대로였으나 test는
`8ae7d027bdbd1785c9bfa5f5654746b38395d73317c60296d425e41c77612d1f`로 바뀌었다.
위 검사 본문/부재 판정은 읽은51ac pin에 한정한다. 새8ae7 내용·추가 사례·결과는
재조회하거나 기다리지 않았으며 미검증이다. 이 리뷰를 새 test pin의 승인으로 사용하지 않는다.

## 시간·보존 경계

사용자 보정과 `intent-time-budget-correction.json`을 따른다. prior active는 **1902초**이며
720초를 사용하지 않는다. 제외 구간은 **01:35:16Z–13:00:30Z만**이다.
보수적 journal 분석/검증 마감은 **2026-10-16 13:28:48 UTC**, 재설정 없음이다.
13:17:15 UTC 시각 확인 시점의 보수 누적은 1902+1005=2907초이며 이후 작업 시간도 같은
마감에 포함한다. runtime D18 HELD는 별개로 계속 유지하며 새 조사 예산을 만들지 않는다.

이번 reviewer는 이 검토 파일만 작성했다. 제품·검사·fixture·계획·원본11개 변경·이전 결과와
최초 오류를 수정하지 않았다. import/검사·DB/runtime/권한 probe·socket/credential 접근,
정책 변경·다른 agent 조회/polling/wait·위임·통지도 수행하지 않았다.
