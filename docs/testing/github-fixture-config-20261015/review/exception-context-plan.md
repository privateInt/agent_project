# L01 호출자 예외 문맥의 정상 load 수명 보정 계획

ID: **L01-exception-context-v1**. 작성: 별도 test_agent. 상태: 독립 사전 검토 대기.
현재 새 검사 추가·이 문제의 제품 수정/재현 실행은 하지 않았다.

기준 제품 SHA-256은 `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122`다.
fixture 정리는 별도 [승인 계획](fixture-fix-plan.md)/[승인](fixture-fix-review.md)에 따라 끝났고,
helper만 수정한 검사 SHA는 `a2e3b571b4995ed039f395241a4d2daccfe4826838b070a64ef5abc3d7ed1b6c`다.
[한정 재검증](../evidence/fixture-retry-1/result.json)은 두 wrapper methods/선택30개
PASS, FAIL·ERROR·SKIP0, 실제 child0이다. 이 검사를 이번 문제 때문에 반복하지 않는다.

## 문제와 최소 제품 변경

[stdlib 관찰](../evidence/exception-context-observation.json)의 SHA-256은
`18f54d7ec398707c75977aec2b8f543f8090e7827321966572ee43f4086ca26d`다.
이는 제품 실행 결과가 아니라 호출자 except의 exception이 정상 반환 함수 안에서도
`sys.exc_info()`에 보이는 Python 관찰이다. 현재 load의 finally는 이를 로컬 load 실패로
오인할 수 있어 검증에 성공한 handle까지 닫는다. 실제 제품 재현은 아래 새 검사로 따로 남긴다.

coordinator만 `load_fixture_controller()`의 로컬 완료 flag를 수정한다. 제안 exact diff는
다음 세 지점이다. 실제 반영 source/diff는 별도 최종 독립 검토 대상이다.

```diff
     handle = None
+    succeeded = False
     try:
         # 기존 설치/상태/DB 두 개/연결/실제 schema 검증은 전부 그대로
         handle._check()
+        succeeded = True
         return handle
     # 기존 except 두 분기 그대로
     finally:
-        if handle is None or len(connections) != 2 or sys.exc_info()[0] is not None:
+        if not succeeded:
             # 기존 handle invalidation과 _release 및 정리 실패 reason 그대로
```

위 정확한 세 변경의 예상 제품 SHA는
`63f5faf6a0bb828928d14bc97a173388ffaca8829f7bad4621a6981e8f180e88`다.
flag는 기대값/설정 검사 뒤 리소스 구간 시작에서 False로 만들고, 최종 `_check()`가 성공한
뒤에만 True로 설정한다. 그 이전 어떤 실패도 기존 정리 body를 수행한다. 외부 except의
존재는 이 판단에 영향을 주지 않는다. `sys`는 runtime module 조회에 필요하므로 import를
삭제하지 않는다. 제품의 다른 함수·클래스·SQL·reason·guard·신뢰 flag·공개 API는 불변이다.

## 새 검사: 공개 load/read 한 사례

제안 이름: `FixtureConfigurationTests.test_l12_load_read_inside_caller_except`.
기존 새 검사 파일에 `Harness.caller_exception_context` 하나, 그 dispatch entry와 method
등록 entry만 추가한다. 기존23개 target/입력/기대값/정리 helper는 바꾸지 않는다.
정상 fixture는 기존 fresh protected installation, marker/version, exact7 외부 개발 pin,
전용 state0700과 명시 setup API로 만든 두 기존 DB0600을 그대로 사용한다.

검사의 입력/행동과 고정 assertion은 다음과 같다.

1. 무해한 fixture RuntimeError 인스턴스를 명시적으로 raise하고 그 **caller except 블록
   안에서** 공개 load를 호출한다. load 전후 `sys.exc_info()[1] is caught`를 확인해 실제
   외부 예외 문맥이 살아 있음을 기록한다. owner 승인값을 만들지 않는다.
2. 같은 except 블록 안에서 반환 handle의 공개 `read('exception-context:absent', 'a'*64)`를
   호출한다. 전용 DB는 비어 있으므로 정답은 기존 controller report의 **JOB_NOT_FOUND**이며
   record/artifact는 None이다. fixture-only/head/9trust flag 의미도 그대로다.
3. read가 FixtureConfigurationError를 내면 reason을 저장하고 모든 자원을 finally에서
   닫은 뒤 `load/read inside caller except must remain usable`이라는 명시 assertion을
   실패시킨다. 기존 코드의 HANDLE_CLOSED를 harness ERROR로 흘리지 않는다. 첫 재현은
   정확 **method1 / semantic FAIL1 / ERROR0 / SKIP0**이어야 한다.
4. 정상 read 뒤 두 builtin connection이 사용 가능한지 확인하고 명시 close 후에는 연결이
   실제 closed, loader-owned FD는 닫혔는지 확인한다. 공개 read/SQLite는 실제 구현이다.
   caller except exception identity, read reason/오류, 연결 수명, child0, collector0, 제품
   init0, DB bytes/hash/행과 fixture tree 불변을 소형 관찰에 남긴다.

오류를 잡는 것은 재현을 assertion FAIL로 분류하기 위해서이며 HANDLE_CLOSED를 통과로
바꾸기 위해서가 아니다. 수정 후에는 같은 assertion/입력의 동일 test bytes로 PASS1을
요구한다. read를 except 바깥으로 옮기거나 기대 reason을 바꾸지 않는다. 테스트의 source
mapping 중 loader 개발 pin만 실제 old/fixed source bytes에 결합하며 다른6개 source,
fixture marker/version·경로 정책·기대값은 변경하지 않는다.

## 실행 순서와 보존

이 계획의 버전/SHA를 독립 reviewer가 승인한 뒤에만 test_agent가 위 한 target을 추가한다.
추가 후 test SHA를 고정하고 **제품 수정 전에** old product487dc146에서 target 하나만
실행하여 semantic FAIL1/ERROR0의 최초 재현 로그·JSON·코드/검사 사본을 새 경로에 남긴다.
그 뒤 coordinator가 위 제품 flag를 보정하고 test_agent는 실패했던 동일 target 하나만
다시 실행하여 PASS1/FAIL·ERROR·SKIP0을 새 경로에 남긴다. 두 실행 모두 actual child0이다.

예시 근거 경로는 `evidence/exception-context-before/`와 `exception-context-after/`다.
기존 run-1·16 ERROR·fixture-retry-1·변이·원래 plan/review/최초 오류 기록은 덮어쓰지 않는다.
실행 도구는 이 작업 root에 두고 실제 target·fresh harness 명령/cwd·Python/SQLite/UID·
fixture root·제품/test/계획/승인 SHA·관찰·FAIL/ERROR/SKIP를 기록한다. local flag 제품 수정과
실패 target 재검증을 같은 문제 D17의 한 회로 세고 D18도 누적한다. native 조사 보류는 유지한다.

기존22 methods의 최초 PASS, 보완30개 PASS, L17 run16개와 L09 첫2개, 실제 child3회,
M1/M2, 과거13+3/R03/R04/R15 및 전체 suite는 재실행하지 않는다. 이번 old-source 재현은
선정된 세 번째 변이가 아니라 새로 발견한 실제 버그의 단일 regression 재현이다.
기존 정상·오류 정리 근거는 해당 source/test pin의 원래 결과로만 재사용하고 최종 source에서
모든 검사를 실행했다고 합산하지 않는다.

## 변경 불변성·기존 오류 정리 의미

제품 diff는 해당 load 정의의 flag 초기화/최종 성공 표시/finally 조건에 한정한다.
AST source-segment와 raw diff를 대조해 그 밖의 모든 정의가 byte 불변인지 확인한다.
load 내부에서도 초기 expectation/config/path guard, 설치/namespace/hash/owner/mode/sidecar/
header/inode/schema/연결 검사, except와 `_release` body/정리 실패 reason은 byte 불변이어야 한다.
새 flag가 final `_check()`보다 먼저 True가 되거나 error 경로에 return이 추가되면 허용하지 않는다.

기존 로컬 실패에서는 모든 raise가 succeeded=False 상태에서 기존 cleanup body로 이어진다.
최초 L12의 첫/둘째 connect 실패 및 중간 거부·close 자원 관찰, fixture 보완 결과와 이 한정
control-flow 대조로 **정상 error-cleanup 의미가 그대로임**을 독립 reviewer가 확인한다.
해당 body/분기까지 바뀌면 이 계획을 벗어나므로 추가 근거 필요성을 다시 판단한다.

검사는 기존 정의를 바꾸지 않고 위 한 method/helper와 dispatch/등록만 추가했는지 대조한다.
최초23개 method 정의와 fixture helper,30개 label/assertion,두 mutation target을 보존한다.
필요한 audit를 실제 수정 diff와 별도 JSON에 남기고 새 PASS와 이전 결과를 구분한다.

사전 승인은 구현/실제 실행의 최종 승인이 아니다. 최종 기능 완료에는 이 한정 제품/검사
diff와 최초 FAIL·수정 PASS·재사용 근거에 대한 별도 독립 검토가 필요하다.
운영 설치/실제 owner값/supervisors/issuer/서명/required-check는 계속 NOT_RUN이다.
