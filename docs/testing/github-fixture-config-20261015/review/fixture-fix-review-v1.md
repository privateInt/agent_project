# L01 fixture 보정 계획 독립 검토

판정: **REQUEST_CHANGES — 계획의 literal 수정 위치와 예상 SHA를 바로잡아야 함**.
개념적인 fixture 정리 보정·한정 30개 재검증은 사소한 동등 보정으로 판단하지만,
현재 계획의 정확한 diff/pin은 그 보정을 하지 않으므로 아직 수정·재실행을 승인하지 않는다.
이 판정은 제품이나 최종 실행 근거의 승인도 아니다.

| 검토 대상 | SHA-256 |
|---|---|
| `review/fixture-fix-plan.md`, L01-fixture-fix-v1 | `3afab3b7cb7c55247198fd00f7eb1c76a260e33f09e8baa992110b99dfa906fb` |
| 제품 source, 이번 보정에서 불변 | `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122` |
| 최초 검사 source | `2ef178fde43ab801e03dd84448bf933457de77799c047402b89c2f8412ed2a02` |
| 최초 result.json | `faadf72d664a4756d292e4cd80df3335caeef64bfaa8b608d9f4a0bb1d706cdb` |
| 최초 unittest.log | `2b61a92d1dfaee24380c337cfee8c000138f708aecf68ba30420e1e1dbd69531` |
| 최초 mutations.json | `774a10a2f29cc53216b2ee500e669430c05ccbcd68f6c8a88da6dd351eeebe6b` |
| 최초 test-agent-result.json | `9338c159230ffb19e90d9f47f0ede058053fa5c5372b3bef3c5e9ce495f26772` |

## 수정이 필요한 한 건: 예상 hash가 cleanup 대신 생성 전 guard 변경에 결합됨

최초 검사 `altered()`의 실제 68행과 81행은 순서와 들여쓰기가 다르다.

```python
    if absent or symlink is not None or fifo:       # 68: 생성 전
        path.unlink(missing_ok=True)
    # fixture 생성과 yield
    finally:
        if symlink is not None or fifo or absent:   # 81: 정리
            path.unlink(missing_ok=True)
```

계획의 before는 `if absent or symlink is not None or fifo:`이고 예상 검사 SHA는
`2021e158b99bc8286a65ba719a7ed415c7f3142d3f236881a3a49c16fab6a4c9`다.
제품·검사를 실행하지 않고 최초 파일의 bytes만 메모리에서 대조한 결과, 이 SHA는
68행의 생성 전 조건만 확장하고 81행을 그대로 두는 결과와 정확히 일치했다.
새 파일을 만들기 전에 없는 파일을 unlink하는 것으로는 yield 뒤 남는 파일을 정리하지
못한다. 이 pin으로 구현하면 L17 실패와 L09의 가려진 독립 검증 문제가 해결되지 않는다.

권고하는 정확한 최소 수정은 **81행 finally만** 다음처럼 확장하는 것이다.

```diff
-        if symlink is not None or fifo or absent:
+        if symlink is not None or fifo or absent or (raw is not None and not existed):
             path.unlink(missing_ok=True)
```

이 한 줄만 바꾼 예상 SHA-256은
`a2e3b571b4995ed039f395241a4d2daccfe4826838b070a64ef5abc3d7ed1b6c`다.
68행은 불변이다. 동일 의미의 순서 변경을 굳이 추가하지 않는 이 형태를 권고한다.
계획의 exact diff와 예상 hash를 맞추고 원래 v1을 보존한 새 계획 pin으로 정정한 뒤
독립 승인을 받아야 한다. reviewer가 제품/검사를 직접 고치지는 않았다.

## 나머지 범위에 대한 판단

최초 결과는 23 methods 중22 PASS, L17 read의16 ERROR, FAIL0/SKIP0이며 subcase264개 중
PASS248/ERROR16이다. 첫 로그에서 read 사례는 변경 주입 전 `h=self.load()`의 sidecar
검사에서 DATABASE_PATH_INVALID로 중단했다. 정리 helper가 남긴 run-sidecar와 합치되며,
목표 read guard를 검증한 결과로 세지 않는다. 최초 결과·로그를 덮어쓰지 않는다.

L09의 최초 ledger-journal empty/link 두 개는 유일한 목표 sidecar를 검사한 기존 근거로
재사용한다. 뒤14개는 남은 journal에 의해 목표 검사에 도달했는지 불명확하므로 계획의
정확한14개 목록만 다시 확인하는 것이 필요하다. 최초 PASS 표기는 보존하되 새 독립
검증 근거와 구분한다. L17의 run16개는 재사용하고 ERROR였던 read16개만 확인한다.

L05 제외안은 수용한다. 초기 namespace initializer 파일이 남는 helper 오류 자체는
감사에 기록해야 한다. 다만 현재 제품 `_installation()`이 `_runtime_paths()`를 먼저
호출하므로 두 namespace 경로 사례의 잘못된 __path__/spec 검색 위치는 잔여 initializer의
존재 검사에 도달하기 전에 각각 직접 거부된다. 현재 source가 불변이고 이 실행 순서를
검토했으므로 L09와 달리 목표 guard 미도달로 분류하지 않는다. 기존 결과와 소스 대조를
재사용하며, 수정 후 깨끗한 fixture에서 두 사례를 재실행했다고 소급하지 않는다.

따라서 정정 후 승인 대상으로 제시할 재검증 범위는 **L09 14개 + L17 read16개 =30개**다.
fresh harness 두 개와 임시 실행 도구의 callable 필터를 사용하는 방식을 수용한다.
첫 사례부터 sidecar가 없는 독립 fixture이고, 각 선택 사례에서는 해당 목표만 존재하며
끝나면 fixture tree가 원상복구됐음을 확인해야 한다. 선택30개·중복0·대상외 callable0,
child0, FAIL/ERROR/SKIP0과 코드·검사·계획·승인 pin을 별도 근거로 남긴다.

원래22 method PASS·L17 run16개·L09 첫2개·실제 child3회·M1/M2·기존13+3/R03/R04/R15·
전체 suite는 반복하지 않는다. M1/M2가 겨냥한 source/pin·state0755 입력은 helper의 새 raw
파일 정리와 무관하여 기존 변이 근거를 재사용할 수 있다. 정상/변이 전체를 새 test SHA에서
실행한 결과로 합산하지 않는다. 필수 기준·reason·guard를 약화하는 보정은 허용하지 않는다.

## 검토 도구 기록과 경계

reviewer의 첫 두 텍스트 일치수 확인은 계획에 적힌 before가 실제 finally와 다르다는
AssertionError였다. 이후 원문의 68/81행을 읽고 메모리의 문자열 대체·SHA만 대조해 위
차이를 확인했다. 이 두 메타데이터 확인 오류는 제품/검사 실행 오류와 별개이며 최초
23 methods의16 ERROR에 합산하지 않는다. 디스크의 코드·검사는 계속 원문 그대로다.

작성한 파일은 이 검토 문서 하나다. 제품/검사 실행·수정, 테스트 기준 완화, 추가 변이,
credential/network/운영/권한 변경, 추가 위임·MCP 통지는 수행하지 않았다. 현재 v1의
수정·재실행 승인은 보류하며, 정정된 exact 계획을 재검토한 뒤 판단한다.
