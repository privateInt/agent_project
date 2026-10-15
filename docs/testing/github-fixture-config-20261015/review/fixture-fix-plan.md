# L01 fixture 정리 수정과 한정 재검증 계획

ID: **L01-fixture-fix-v2**. 작성자: 별도 test_agent.
상태: 독립 사전 검토 대기. 이 계획 작성 중 제품·검사 소스를 수정하거나 재실행하지 않았다.
기준은 [승인 계획](test-plan.md), [사전 승인](plan-review.md),
[최초 결과](../evidence/run-1/result.json), [최초 오류 분석](../evidence/first-error-analysis.json)이다.

최초 v1은 [원문](fixture-fix-plan-v1.md), SHA-256
`3afab3b7cb7c55247198fd00f7eb1c76a260e33f09e8baa992110b99dfa906fb`로 보존했다.
[독립 검토](fixture-fix-review.md)의 REQUEST_CHANGES를 반영해 exact diff의 before/after와
예상 test SHA만 정정했다. v1은 생성 전 unlink 조건을 가리키는 잘못된 문자열을 사용했다.
이 v2는 실제 finally의 81행만 바꾸고 생성 전 68행은 그대로 둔다. v1의 잘못된 보정은
제품·검사 소스에 적용하거나 실행하지 않았다. 기존30개 선정·기대값·한계는 변경하지 않는다.

| 대상 | 보존 pin |
|---|---|
| 제품: 이번 계획에서 변경 없음 | `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122` |
| 최초 검사 원문 | `2ef178fde43ab801e03dd84448bf933457de77799c047402b89c2f8412ed2a02` |
| 최초 unittest.log | `2b61a92d1dfaee24380c337cfee8c000138f708aecf68ba30420e1e1dbd69531` |
| 최초 result.json | `faadf72d664a4756d292e4cd80df3335caeef64bfaa8b608d9f4a0bb1d706cdb` |
| 원래 계획 | `b350192d28825de5761c448ddff6f73f00dcf778fceec164c2f2134eda353d50` |

## 관찰과 원인

최초 23 methods 중22개가 PASS로 보고됐고, L17의 read subcase16개가 ERROR였다.
최초 subcase 기록은264개: PASS248/ERROR16/FAIL0이다. 이 원문을 고치거나 PASS로 덮어쓰지 않는다.
실제 child3회와 두 변이의 각 assertion FAIL1/ERROR0도 원래 실행 근거로 보존한다.

`altered(path, raw=...)`는 없는 파일에 raw bytes를 쓸 수 있지만, finally에서 absent/symlink/
fifo 조건만 정리한다. existed=False이면 saved와 old_mode도 None이어서 새 regular file이
남는다. L17의 마지막 run-sidecar는 올바른 거부를 확인한 뒤 ledger.sqlite-wal을 남겼다.
이어지는 read16개는 목표 변경 주입 전 load에서 sidecar를 올바르게 거부해 ERROR가 났다.
제품의 잘못된 수용/쓰기/child 실행은 이 오류에서 관찰하지 않았다.

## 최소 수정: 검사 helper 한 조건

수정 파일은 `workspace/src/ops/tools/tests/test_github_fixture_config.py` 하나이며,
`altered()`의 아래 finally 조건만 확장한다.

```diff
-        if symlink is not None or fifo or absent:
+        if symlink is not None or fifo or absent or (raw is not None and not existed):
             path.unlink(missing_ok=True)
```

기존 파일의 raw/mode 복원, 기존 missing/symlink/FIFO 정리, 제품 호출·기대 reason·assertion·
target/입력 생성은 그대로다. 이 정확한 한 줄 변경의 예상 검사 SHA-256은
`a2e3b571b4995ed039f395241a4d2daccfe4826838b070a64ef5abc3d7ed1b6c`다.
실제로 적용한 뒤 원문 diff/AST source-segment 비교로 `altered` 이외 모든 정의의 bytes
불변을 확인한다. 처음 검사 사본은 `evidence/run-1/source-and-tests/`에 이미 보존돼 있다.

## 모든 altered 호출 감사

최초 검사 원문의 AST에서 `altered()` 호출 위치14개를 읽기 전용으로 대조했다.
아래 위치는 그 고정 원문의 줄 번호이며 shared helper의 단순 호출 횟수나 test method 수가 아니다.

| 원문 위치 | 사용과 남을 수 있는 파일 | 판정·필요한 조치 |
|---|---|---|
| 275,280,283 | L03의 기존 marker raw/kwargs/version | 대상은 원래 존재한다. saved 복원 또는 기존 absent/symlink/FIFO 분기로 정리된다. 다른 marker 원인을 가린 잔여 파일 없음. 재실행 제외 |
| 299 | L04의 기존 audit 파일 kwargs | 원래 파일이 있고 missing/mode/FIFO 복원 경로가 동작한다. pin 음성 사례는 altered 자체를 쓰지 않는다. 재실행 제외 |
| 319 | L05의 새 tools/__init__.py empty raw | 초기 파일이 없어 남는다. 다만 이후 두 namespace-path 사례는 `_installation`이 먼저 호출하는 `_runtime_paths`에서 각각 잘못된 __path__/spec 검색 위치를 직접 거부한다. initializer 존재 검사는 그 다음이므로 최초 PASS를 가리지 않는다. 앞선 root/module-origin 사례는 initializer 주입 전에 실행됐다. 이 제품 순서와 기존 결과를 근거로 L05 재실행 제외안을 reviewer에게 제출 |
| 401 | L08의 기존 DB kwargs | 기존 두 DB를 대상으로 복원한다. 외부/nested/alias fixture는 명시 setup·별도 finally로 관리된다. sidecar 생성 없음. 재실행 제외 |
| 422의 두 호출 | L09의 새 sidecar empty raw 또는 symlink | 최초 ledger-journal empty는 독립된 유효 PASS. 바로 뒤 journal-link는 **동일 경로를 symlink로 교체**해 유일 sidecar가 목표 링크이므로 유효 PASS. 이후 14개는 남은 ledger-journal 등이 별도 거부 원인이 될 수 있다. PASS라는 원래 표기는 보존하지만 독립 target 근거로 사용하지 않고, 아래14개만 재검증 |
| 433 | L10의 기존 DB raw/header | 대상 DB는 원래 존재하여 bytes를 복원한다. 실제 closed-WAL fixture는 별도 명시 finally로 DB를 복원하고 sidecar 부재도 assert했다. L09와 fresh installation/process가 분리돼 L09 잔여 sidecar가 들어오지 않는다. 재실행 제외 |
| 644,645,646,647 | L17의 기존 source/marker/state-mode/DB-mode/header | 기존 파일·디렉터리의 raw/mode를 복원한다. L17 run의 성공16개 중 이 사례들은 독립된 PASS다. 해당 run 사례 재실행 제외 |
| 648 | L17의 새 -wal empty raw | run-sidecar PASS 뒤 잔여 파일로 read16개가 목표 주입 전에 ERROR. run-sidecar PASS는 재사용하고 미도달 read16개만 재검증 |

L01/L02/L06/L07/L11~L16/L18/L19는 이 helper의 새 raw file 경로로 거부 원인이 오염되지 않았다.
각 outer method는 fresh installation/process를 사용하므로 L09/L17 잔여물이 다른 method로
전달되지 않는다. L20의 원본 보존·최종 게시 확인은 coordinator 책임을 유지한다.
M1은 bad external pin만, M2는 실제 state chmod만 사용하여 이 정리 오류와 무관하다.
두 변이는 다시 실행하지 않는다. source/runtime/OS 권한 기대값을 완화하지 않는다.

## 정확한 재검증 집합: 30개만

**L17:** 최초 ERROR였던 아래 read16개만 실행한다. run16개는 모두 제외한다.

```text
read-src/__init__.py
read-src/ops/__init__.py
read-src/ops/tools/github_check_audit.py
read-src/ops/tools/github_fixture_collector.py
read-src/ops/tools/github_fixture_ledger.py
read-src/ops/tools/github_fixture_controller.py
read-src/ops/tools/github_fixture_config.py
read-marker
read-state
read-ledger-mode
read-ledger-inode
read-control-inode
read-state-inode
read-install-inode
read-header
read-sidecar
```

**L09:** 다른 sidecar에 의해 가려질 수 있었던 아래14개만 실행한다. 최초 ledger-journal
empty/link 두 PASS는 독립 근거로 재사용하고 반복하지 않는다.

```text
ledger.sqlite-wal-empty
ledger.sqlite-wal-link
ledger.sqlite-shm-empty
ledger.sqlite-shm-link
ledger.sqlite-mjL01-empty
ledger.sqlite-mjL01-link
control.sqlite-journal-empty
control.sqlite-journal-link
control.sqlite-wal-empty
control.sqlite-wal-link
control.sqlite-shm-empty
control.sqlite-shm-link
control.sqlite-mjL01-empty
control.sqlite-mjL01-link
```

L09 재검증이 필요한 이유는 새 실패가 아니라 최초 fixture의 잔여 파일 때문에 그14개 target의
독립 검증력이 불명확하기 때문이다. 이를 이유 없이 통과 검사 전체를 반복하는 것으로 확장하지 않는다.
원래22개 method PASS·L17 run16개·L09 첫2개·실제 child3회·M1/M2 및 기존13+3/R03/R04/R15는
재실행하지 않는다. L05 제외안이 reviewer에게 수용되지 않으면 그 두 namespace label에 관한
근거와 최소 추가 범위를 먼저 수정 계획으로 확정하며 지금 임의로 실행하지 않는다.

## 실행 도구와 통과선

사전 승인 후 `retry_l01_fixture.py` 등 **작업 root의 임시 도구만** 추가한다. committed 검사에
필터 API나 historical ready-marker 의존성을 추가하지 않는다. 도구는 기존 새 test module을
읽어 fresh protected installation을 준비하고 각 별도 child에서 같은 `Harness`를 사용한다.
`Harness.check`를 그 child의 in-memory wrapper로 감싸 위 label의 callable만 원래 check에
전달한다. `sidecars()`/`live_changes()`를 직접 호출하여 outer check에 선택이 막히지 않게 한다.
선택되지 않은 callable은 실행하지 않는다. 제품 함수·기대값·원래 assertion은 교체하지 않는다.

두 fresh process에 각각 L09-14개와 L17-read16개를 전달한다. loop에서 label/callable을
만드는 것과 실제 callable을 실행하는 것을 구분하고, 실행된 label 목록이 위 집합과 정확히
같으며 중복0인지 assert한다. 각 선택 case 전후 fixture tree를 비교해 새 파일 정리와 기존
bytes/inode/mode 보존을 확인한다. 보호 대상에 대한 제품 쓰기·child는 계속0이어야 한다.
결과는 두 focused wrapper methods/30 selected checks로 기록하며 원래23 methods 전체를
새 test SHA에서 PASS했다고 합산하지 않는다. label filtering·실제 fresh harness 명령/cwd와
코드·검사·수정 계획·승인 SHA도 근거에 남긴다.

통과선은 선택30개 PASS/FAIL·ERROR·SKIP0, 실제 child0, 대상외 callable 실행0, 제품 source
SHA 불변, helper 외 기존 test bytes/기대값 불변이다. 새 로그/JSON은 예컨대
`evidence/fixture-retry-1/`에 새로 만들며 최초 run-1/변이/오류 원문을 덮어쓰지 않는다.
추가 실패면 최초 기록과 해당 label을 보존하고 중단·보고하며 통과 label을 반복하지 않는다.
D17/D18의 같은 문제 상한은 누적하고 기존 native sandbox 소진 보류를 유지한다.

이 수정은 fixture 원상복구와 불명확한 검사 근거의 보완이다. 제품·권한·guard·정책·필수 기준을
변경하지 않는다는 판정을 독립 reviewer에게 요청한다. **승인 전 helper 수정이나 재실행은 없다.**
