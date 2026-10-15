# L01 fixture 보정 v2 독립 사전 검토

판정: **PASS — L01-fixture-fix-v2의 fixture 정리와 정확히30개 한정 재검증만 승인**.
제품이나 최종 기능 인수의 PASS가 아니다. 별도로 발견된 caller exception context 문제도
이 승인으로 해소되거나 검증된 것으로 취급하지 않는다.

| 대상 | SHA-256 |
|---|---|
| 승인 계획 `review/fixture-fix-plan.md`, L01-fixture-fix-v2 | `cbebfff754409a0fa32767b88d706ed672ad3c2973a7d31d491e1b4443055ddb` |
| 보존 v1 계획 | `3afab3b7cb7c55247198fd00f7eb1c76a260e33f09e8baa992110b99dfa906fb` |
| 보존 최초 REQUEST_CHANGES `fixture-fix-review.md` | `e1df7efcfa250191133f01a04e820179d8a8d35033f6231fea02384f609630c8` |
| 이번 fixture 보정의 제품 pin | `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122` |
| 최초 test pin | `2ef178fde43ab801e03dd84448bf933457de77799c047402b89c2f8412ed2a02` |
| 승인된 한 줄 수정 후 예상 test pin | `a2e3b571b4995ed039f395241a4d2daccfe4826838b070a64ef5abc3d7ed1b6c` |

v1과 v2의 실제 문서 diff를 읽었다. v2는 버전·정정 이력을 추가하고 실제 finally의
81행과 일치하는 before/after 및 기대 hash로 바로잡았다. 68행의 생성 전 조건은 불변이다.
앞선 REQUEST_CHANGES와 v1 원문은 그대로 보존돼 있다. 잘못된 v1 hash를 제품/검사에
적용하거나 실행하지 않았다는 기록과, 검토 시 여전히 최초 test bytes인 상태를 구분한다.

승인하는 변경은 아래 한 줄뿐이다. 기존 raw/mode 복원과 다른 helper·target·입력·기대값·
assertion 및 제품 코드를 바꾸지 않는다.

```diff
-        if symlink is not None or fifo or absent:
+        if symlink is not None or fifo or absent or (raw is not None and not existed):
```

이는 테스트 소유의 새 raw fixture 파일만 context 종료 때 제거해 처음 상태로 되돌리는
보정이다. 제품·guard·권한·필수 기준을 바꾸지 않는 사소한 동등 보정으로 판단한다.
최초 test bytes를 메모리에서 위 한 줄로만 바꿨을 때의 예상 SHA를 대조했으며 제품이나
테스트를 실행한 것은 아니다. 적용 후 실제 diff와 altered 외 정의 원문 불변을 기록한다.

실행은 계획에 열거된 **L09의 가려질 수 있던14개와 L17의 ERROR였던 read16개**만 허용한다.
선택30개·중복0·대상외 callable0을 확인하고, 별도 두 fresh harness에서 처음 sidecar가
없고 각 사례의 목표 파일만 존재하며 끝나면 fixture tree가 복구됐음을 확인한다.
기준은 PASS30 / FAIL·ERROR·SKIP0, child0 및 제품 source 불변이다. 임시 필터 도구로
선정 callable만 호출하고 committed 검사에 필터 API나 원래 assertion 변경을 넣지 않는다.

L09의 첫 journal empty/link 두 개, L17 run16개, 원래 다른22 method PASS, 실제 child3회,
M1/M2, 기존13+3·R03/R04/R15·전체 suite는 재실행하지 않는다. L05의 두 namespace 경로
사례는 현재 소스 순서상 `_runtime_paths`가 잔여 initializer 검사 전에 직접 거부했으므로
최초 결과와 정적 근거를 재사용한다. 깨끗한 fixture에서 해당 사례를 새로 실행했다고
소급하지 않는다. 원래22 PASS 중 L09의 독립14개 검증력은 이 새 근거로 보완한다.

최초 run-1의23 methods/264 subcases, PASS22 methods·PASS248 subcases·ERROR16, 최초 로그,
변이 결과·메타데이터 도구 오류와 v1 문서는 보존한다. 새30개는 별도 결과·로그와 실제
source/test/계획/승인 pin에 연결한다. 새 test SHA에서 원래23 methods 전체가 통과한
것으로 합산하지 않는다. 추가 오류가 나면 그 최초 기록과 해당 label을 보존하고 중단·보고한다.

이 reviewer는 제품/검사 파일을 수정하거나 import·실행하지 않았다. 본 사전 검토 문서만
추가했다. 최종 구현/실행/변이 근거 검토, caller exception context 문제와 그 새 좁은 계획의
검토는 별도이며, 기존 sandbox/운영/credential/권한/무인실행 보류도 유지한다.
