# C01 독립 최종 검토

판정: **수정 필요 — REQUEST_CHANGES**. 아래 P2 한 건 때문에 현재 source pin의 최종 인수는 승인하지 않는다. 기존 13개 검사 통과 기록은 그대로 유효하며, 이 검토에서는 검사를 실행하거나 제품·검사·기존 근거를 수정하지 않았다.

## 수정이 필요한 구현 결함

### P2 — 저장 후 충돌 조회의 비공개 반환 규칙을 우회함

대상: `workspace/src/ops/tools/github_fixture_controller.py:409-412`.

artifact 기록이 성공한 뒤 후속 `read_fixture_controller_job`이 control pin 불일치 또는 terminal 실패와 sealed artifact의 모순을 발견하면 `STATE_CONFLICT`, `record=None`, `artifact=None`으로 차단한다. 그러나 `run_fixture_job`은 후속 reason이 `ARTIFACT_STORED`가 아니라는 조건만으로 `observed["artifact"] = artifact`를 수행한다. 이 분기는 `_result`의 충돌 시 비공개 처리(253-254행)를 다시 우회한다.

따라서 최초 관찰과 기록 후 재조회 사이에 control 상태가 충돌한 경우, 최종 응답은 `BLOCKED/STATE_CONFLICT`이면서 원래 artifact bytes를 반환할 수 있다. 이는 독립 사전 검토의 “Rejected job/pin or inconsistent-state requests return no artifact/record disclosure” 계약에 어긋난다. 여기서 지적하는 것은 DB 소유자의 공격을 방어한다는 새 요구가 아니라, 이미 선택한 충돌 반환 계약을 같은 API의 마지막 분기에서도 유지해야 한다는 점이다.

필요한 수정: 후속 조회 결과에 artifact를 복원할 때도 `_result`와 동일한 충돌·pin·assignment 비공개 규칙을 적용한다. DB/응답 유실 같은 불명 상태에서 원래 artifact bytes를 보존하는 C09 계약은 유지한다. 저장 직후 후속 조회에 충돌이 드러나는 좁은 회귀 사례를 추가해 reason/status와 `record=None`, `artifact=None`을 함께 확인한다. 기존 C10은 호출 전부터 존재한 충돌을 확인하므로 이 마지막 복원 분기에 도달하지 않는다. 이 문제는 정적 경로 대조로 확인했으며 재현 검사를 실행한 것으로 보고하지 않는다.

새 회귀와 이 분기 변경의 영향을 받는 C09만 필요한 범위에서 확인하면 된다. 기존 R03/R04/R15, 무관한 통과 검사, 전체 suite 및 held sandbox 진단을 재실행할 근거는 없다.

## 대조한 근거와 확인 범위

- 정확한 사전 계획과 독립 사전 검토, 새 controller 417행, 새 검사 629행, 계약 46행 및 지정 실행·변이·보존 근거를 읽었다. 이유는 열거된 **15개**, report 키는 20개다. 사전 검토 문장 중 “16”은 전달받은 단순 개수 오기로 처리하며 새 이유를 추가하지 않는다.
- `evidence/run-1/result.json`과 원래 `unittest.log`는 Python 3.10.12 / SQLite 3.37.2 / UID 1011에서 새 검사 13 methods, failures/errors/skipped 모두 0으로 일치한다. 과거 Python 3.12 개발 결과를 이번 실행으로 합산하지 않았다.
- 실제 관찰: C02의 고정 ok/exit7 수집·claim 선행·수집 중 열린 transaction 없음·재개방/캐시 bytes/hash/binding 일치, C03의 실제 파일 부재·Popen 0·복구 후 terminal 유지, C05의 실제 exit70 후 재수집 0, C06의 실제 두 프로세스/collector entry 하나, C11의 실제 busy lock 약 0.102초를 검사 구현과 실행 근거에서 확인했다.
- C04 및 C07-C09는 주입한 결과·예외·COMMIT 응답 유실이다. 실제 process/SQLite 관찰과 구분돼 있다. claim COMMIT 불명 시 collector 0, 준비 실패 전이/기록 COMMIT의 전후 관찰 및 기록 불명 시 원래 bytes 보존 경로가 각각 별도 검사로 연결된다.
- M1은 기존 CLAIMED의 재수집을 허용한 변이에서 `collector.call_count`가 1이 되어 의미 assertion FAIL 1/ERROR 0이다. M2는 coherent SPAWN_FAILED의 whitelist 추가로 잘못된 terminal 전이를 시도하며, 고정 SQLite CHECK가 이를 거부해 `DATABASE_UNCONFIRMED != OUTCOME_UNKNOWN` assertion FAIL 1/ERROR 0이다. 두 mutant의 실제 source hash와 새 검사 hash를 대조했다. M2는 잘못된 분류/쓰기 시도 검출이며, terminal 실패가 실제 저장됐다는 근거로 표현하면 안 된다. malformed-response 거부나 import ERROR로 검출된 것은 아니다.
- 기존 audit/collector/ledger/ledger-test 네 파일의 workspace SHA가 동결 pin과 일치한다. `preservation.json`은 원래 repository의 미커밋 변경 11개와 이전 R15 19파일·R03 16파일·R04 15파일 및 최초 오류 기록의 보존을 보고한다. 이 제한된 최종 검토에서는 원래 checkout/이전 실행 디렉터리를 다시 조사하지 않았으므로 해당 외부 비교는 coordinator의 보존 근거로 귀속한다.
- 실행 JSON은 새 테스트 코드 작성자를 `coordinator_after_test_task_waited_on_approval`로 명시한다. test_agent가 사전 계획을 작성했다는 사실과 최종 테스트 코드 작성자는 구분한다. 이 검토는 실제 새 테스트와 근거를 독립적으로 대조했으며, 최종 테스트 코드를 test_agent가 작성했다고 인증하지 않는다.

## 정확한 검토 pin

부모 commit: `e1eff75b47cdf3fae1088d51d35aaea701f81477`.

| 파일 | SHA-256 |
|---|---|
| `review/requirements.md` | `0c10da556e6ee01f689d6c74f5a8d1fe7d2736c2e74cf115bc82fdf38219778f` |
| `review/test-plan.md` | `227df66ea111e1d29e5982290630deac9b3b40b9e034e064a08283540142962e` |
| `review/plan-review.md` | `9caf0395f33563d9fc7fc88247b58b2d3d18dc34df6b9756cba37c6a94d1aa53` |
| `workspace/src/ops/tools/github_fixture_controller.py` | `e96e97fa47c190b5340b3989694f41ee29804d3cf60790e54af2aa13bdcac5be` |
| `workspace/src/ops/tools/tests/test_github_fixture_controller.py` | `fa293ea0686d4dd14b70c73b7c580b61f42d02c0746c65aa28af930fb529d818` |
| `workspace/docs/codex/github-fixture-controller.md` | `4121e22850a4537089f4555b93d30256c0a8ae7a2ad3f98cb930f2ffb9fde2ee` |
| `evidence/run-1/result.json` | `b59aacf7fc931fdc325c61664ff931723507e15e2355836462b9f12bf5d4a7ef` |
| `evidence/run-1/unittest.log` | `a35135119d0cfe5accece064a5b1cdf57e763ad6db0cd2a1459b8f5dd7b1619f` |
| `evidence/mutations.json` | `e8b463b3e3e029df09779a6d8010986e8def1507cd97fb74e12f525fea3c274a` |
| `evidence/preservation.json` | `595b7c05e639795871b59137886667b05961fe7298d7493952acf4e69d7fe10f` |

기존 불변 파일 확인값:

- audit: `d2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e`
- collector: `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0`
- ledger: `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4`
- 기존 ledger tests: `9dc968aef2ae7e8ae5f281b41d853a0d8d370d63b67b654aeabe9fd179087bbe`

범위: fixture controller의 독립 구현/근거 검토다. 보호된 설치·상태 출처·영구 DB pairing·exactly-once process 실행·실제 head 격리·서명/검사 발행·필수 gate·native 준비·병합 승인은 완료로 판정하지 않는다. 이 검토가 작성한 파일은 이 최종 검토 문서 하나뿐이며, 제품/테스트 실행·credentials/settings/network 접근·추가 delegation·통지 호출은 수행하지 않았다.
