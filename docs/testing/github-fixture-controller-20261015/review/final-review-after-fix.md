# C01 P2 수정 후 독립 최종 검토

판정: **PASS — 아래 최종 source/test pin의 fixture-controller 범위**.

최초 `review/final-review.md`의 P2는 해소됐다. 최초 REQUEST_CHANGES는 수정 전 pin에 대한 유효한 기록으로 원문 그대로 보존한다. 이 승인은 보호된 설치·실제 head 실행·서명/검사 발행·native 격리·필수 gate·병합 승인이 아니다.

## 수정과 검사 대조

제품의 실제 전후 diff와 `evidence/p2-source.patch`를 대조했다. 변경은 `_NO_DISCLOSURE`의 고정 세 reason 집합과 이를 사용하는 두 guard뿐이다. 기존 `_result`의 비공개 의미를 유지하고, 저장 후 마지막 조회가 비공개 reason을 반환하면 원래 artifact를 다시 붙이지 않는다. API·상태·SQL·claim 선행 gate·준비 실패 분류·수집기/원장 호출 계약은 변경되지 않았다. 최종 조회가 DATABASE_UNCONFIRMED인 경우의 원래 bytes 보존도 유지한다.

새 테스트 diff는 기존 파일에 helper 하나와 focused test methods 세 개를 추가한 것뿐이다. 수정 전 복사본과의 직접 줄/bytes 비교에서 기존 테스트 파일의 모든 원래 bytes가 삭제·교체 없이 유지되는 것을 확인했다. `p2-diff-audit.json`의 기존 class method definitions 25개는 13개 검사와 helper를 합한 수이며, 별도 검사 25개를 실행했다는 뜻이 아니다.

| 실행 근거 | 정확한 범위와 결과 |
|---|---|
| `evidence/p2-before` | 수정 전 source에서 새 pin 충돌·terminal 모순 methods 두 개만 실행. FAIL 2 / ERROR 0 / SKIP 0. 두 로그 모두 `post-record conflict must not disclose artifact` assertion에서 의미 실패 |
| `evidence/p2-after` | 수정 후 실패했던 두 methods와 처음 실행한 마지막 조회 불명 method만 실행. PASS 3 / FAIL·ERROR·SKIP 0 |

두 실행의 test SHA는 동일하다. 재현 뒤 기대값을 바꿔 통과시킨 것이 아니다. 수정 후 사례는 다음 경계를 실제로 통과한다.

- 두 충돌 사례: 변경 없는 고정 collector와 실제 ledger 기록이 성공한 뒤 테스트가 소유한 control DB에 pin 불일치 또는 PREPARATION_FAILED 모순을 만든다. 실제 후속 read가 BLOCKED/STATE_CONFLICT를 반환하고 최종 run 응답은 `record=None`, `artifact=None`이다. 모순 주입 후 양쪽 원래 행을 보존하며 수집·기록은 각각 한 번이다.
- 마지막 조회 불명 사례: 실제 artifact 기록 성공 뒤에만 control read COMMIT의 정상 호출 후 응답 오류를 주입한다. DATABASE_UNCONFIRMED와 저장 확인 false를 유지하면서 원래 수집 bytes를 caller에게 반환한다. fault 제거 후 DB를 닫고 다시 열어 저장된 bytes/hash를 확인한다. 앞선 claim/기록 COMMIT 오류로 변경 분기를 우회하지 않는다. 오류는 명시적인 주입이며 실제 GitHub 또는 운영 장애 관찰이 아니다.

환경 근거는 Python 3.10.12 / SQLite 3.37.2 / UID 1011, 고정 child `/usr/bin/python3`다. 이전 Python 3.12 개발 결과를 이 실행 결과로 합산하지 않았다.

## 이전 근거 재사용과 보존

기존 13 methods 및 M1/M2는 원래 source/test pin의 성공·변이 근거로 재사용한다. 기존 13 methods, C09, R03/R04/R15, 기존/새 전체 suite, M1/M2를 이번 수정 때문에 다시 실행하지 않았다. 최종 source에서 16 methods 전체를 실행했다고 표시하면 안 된다. 이번 승인 근거는 이전 독립 검토·검사 결과, 변경이 한정된 실제 diff 및 새 3개 focused 결과의 결합이다. M1/M2가 겨냥한 fresh-claim gate와 SPAWN_FAILED 분류는 변경되지 않아 재변이 실행이 필요하지 않다.

`p2-diff-audit.json`은 최초 메타데이터 비교 helper가 빈 줄 경계 재구성에서 낸 AssertionError와 AST 위치 기반 원문 비교로 수정한 사유를 보존한다. 이는 제품 검사 실패/재실행이 아니며, P2의 FAIL 2와 PASS 3 기록은 그대로다. 검사 실패와 메타데이터 도구 오류를 합산하거나 숨기지 않았다.

최초 run-1 결과/로그, mutations, preservation 및 모든 이전 review의 실제 SHA가 최초 검토/승인 때의 값과 일치한다. 기존 audit/collector/ledger/ledger-test 네 파일도 동결 SHA와 일치한다. `preservation.json`의 원래 repository 11개 변경·이전 R15/R03/R04와 최초 오류 기록 보존 보고는 해당 coordinator 비교 근거로 귀속하며, 이 제한된 재검토에서는 원래 checkout이나 이전 실행 디렉터리를 다시 조사하지 않았다.

## 정확한 검토 pin

부모 commit: `e1eff75b47cdf3fae1088d51d35aaea701f81477`.

| 파일 | SHA-256 |
|---|---|
| 최종 controller | `671885e474d4eb2eafe4f786ea8f7075924bbd96250b77ff832561e1b943c5e7` |
| 최종 새 tests | `b765ec07ff8fd4b68b88dd8e4d41aa40680c578844135ceb97518c8a99a2d15a` |
| 기존 계약 `workspace/docs/codex/github-fixture-controller.md` | `4121e22850a4537089f4555b93d30256c0a8ae7a2ad3f98cb930f2ffb9fde2ee` |
| 원래 사전 계획 `review/test-plan.md` | `227df66ea111e1d29e5982290630deac9b3b40b9e034e064a08283540142962e` |
| 원래 독립 사전 검토 `review/plan-review.md` | `9caf0395f33563d9fc7fc88247b58b2d3d18dc34df6b9756cba37c6a94d1aa53` |
| 승인된 v2 delta `review/fix-plan.md` | `68831d156cf633d4aacf1d77a4d54d54fc7a138ada19d06dbac547b4b5c4208b` |
| v2 독립 승인 `review/fix-plan-v2-review.md` | `beeaf3e69b9f98bccd3c9d4f59335c5d3fdb446c6973e4061f0b117cdf6be069` |
| 보존한 v1 승인 `review/fix-plan-review.md` | `af4cde36ed10b0bd3324406dadbf23ab568ecd6750bebdee0da672eb33371da2` |
| 최초 REQUEST_CHANGES `review/final-review.md` | `a5e81fd8dd524a32b01f44172d43122082265d12e3e67bf65a853e19475ea8f6` |
| 수정 전 controller 복사본 | `e96e97fa47c190b5340b3989694f41ee29804d3cf60790e54af2aa13bdcac5be` |
| 수정 전 tests 복사본 | `fa293ea0686d4dd14b70c73b7c580b61f42d02c0746c65aa28af930fb529d818` |
| `evidence/p2-before/result.json` | `d6d82cf334ff16a86b03debe8c8b5aebeb252e3dc7c2fe9489218e5a6b30a206` |
| `evidence/p2-before/unittest.log` | `750b2df431e995f897961d4de00e993ba0ff08f5cedf149a9868f2bc947b47a1` |
| `evidence/p2-after/result.json` | `85259d4ad58958230411a4b70e0d12ff334dde03b91270095b8c1390847be20a` |
| `evidence/p2-after/unittest.log` | `bac7fc5e8e31f587611b4f4a1c79bc61b07d7a71b6270967e718735b1aa3c747` |
| `evidence/p2-source.patch` | `150151fc77e63a7b998482b88eeae227d578e998ff73f97a6abba2d882995ec4` |
| `evidence/p2-diff-audit.json` | `9351c8816f464a4e519cfbf4ada19995ce891f028b508c6ef003bceb8db1f5f5` |

이 최종 검토는 테스트·제품 실행, 제품/검사 수정, credentials/settings/network 접근, 추가 delegation 또는 MCP 통지를 수행하지 않았다. 새로 작성한 파일은 이 최종 검토 문서 하나뿐이다. 남은 실제 설치·controller 설정 출처·역할별 supervisor/issuer·최신 binding 평가/검사 발행 연결은 별도 구현·소유자 설정·인수 범위다.
