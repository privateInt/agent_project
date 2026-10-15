# L01 caller exception context 보정 사전 독립 검토

판정: **PASS — L01-exception-context-v1의 정확한 계획 pin에 대한 사전 승인**.
제품 수정·실제 재현·수정 후 통과·최종 인수 승인은 아직 아니다.

| 대상 | 고정 값 |
|---|---|
| 계획 | `review/exception-context-plan.md`, L01-exception-context-v1 |
| 계획 SHA-256 | `e5da95d45633fbd0b6961bd50e7455bc7dbf4ca746c19ba879eada13572cfaae` |
| 수정 전 제품 SHA-256 | `487dc146b24b302794dc650128f6aca1349e887e28946976148389dfb603e122` |
| 현재 fixture 보정 후 검사 SHA-256 | `a2e3b571b4995ed039f395241a4d2daccfe4826838b070a64ef5abc3d7ed1b6c` |
| 계획한 제품 수정 후 예상 SHA-256 | `63f5faf6a0bb828928d14bc97a173388ffaca8829f7bad4621a6981e8f180e88` |
| stdlib 관찰 SHA-256 | `18f54d7ec398707c75977aec2b8f543f8090e7827321966572ee43f4086ca26d` |
| 별도 fixture-retry-1/result.json SHA-256 | `2cd3b3f54d3b702359b71d033e2c35b7c8bbe4c37cb794803ea15322d379c7f6` |

계획 원문과 현재 load 구현, 별도 stdlib 관찰과 fixture 결과를 읽었다. 현재 source/test
pin이 위 값과 일치하며 새 제품 변경이나 새 검사 추가 전 상태임을 확인했다. fixture 결과의
2 wrappers/선택30개 PASS와 ERROR·FAIL·SKIP0은 이미 별도 실행된 근거로 구분한다.

## 제품 변경 판정

현재 load finally의 `sys.exc_info()[0]`는 callee의 로컬 실패 여부만을 나타내지 않는다.
제공된 stdlib 관찰에서 정상 반환 함수 안에도 caller except의 RuntimeError가 보인다.
따라서 정상 load가 열린 handle을 반환하면서도 현재 finally가 이를 닫을 수 있다는
소스 경로와 일치한다. 다만 stdlib 관찰을 실제 제품 재현 결과로 표시하지 않는다.

다음 세 변경만 승인한다. 원래 bytes를 읽어 메모리에서만 대체했을 때 위 예상 제품 SHA와
정확히 일치함을 확인했다. 디스크의 제품 파일을 바꾸거나 실행한 것이 아니다.

- resource 구간 진입의 `handle = None` 뒤에 `succeeded = False`를 둔다.
- 마지막 `handle._check()`가 성공한 다음, return 바로 전에만 True를 기록한다.
- finally의 조건을 `if not succeeded:`로 바꾸고 기존 invalidation·release·실패 reason
  본문과 두 except 분기, 초기 검증 및 나머지 모든 정의는 그대로 둔다.

기존 자원 구간의 실패는 True 표시 전에 발생하므로 False 상태에서 같은 cleanup 본문으로
이어진다. 초기 기대값/config/path 거부는 자원 취득 전이며 전후 의미가 같다. 정상 반환은
caller의 예외 처리 문맥과 무관하게 열린 handle의 소유권을 전달한다. `sys`는 runtime module
검증에 계속 쓰이므로 import를 지우지 않는다. guard·SQL·공개 API·권한·신뢰 flag·필수 기준을
바꾸지 않는 좁은 결함 보정으로 판단하며, 기존 오류 정리 검사를 반복할 새 이유는 없다.
실제 diff가 이 경계를 벗어나면 이 동등성 판단도 다시 검토해야 한다.

## 새 검사와 실행 순서 승인

새 target `FixtureConfigurationTests.test_l12_load_read_inside_caller_except` 한 개와
그 helper·dispatch·등록 추가만 승인한다. 나머지 기존 정의·입력·기대값은 유지한다.
신뢰한 fresh fixture의 실제 SQLite 두 개를 사용하며 다음 경계를 반드시 확인한다.

- caller except 안에서 실제 RuntimeError identity를 load 전후로 확인하고, 같은 except
  안에서 공개 load→공개 read를 수행한다. read를 블록 밖으로 옮기지 않는다.
- 비어 있는 원장의 `exception-context:absent` 조회는 기존 controller의 JOB_NOT_FOUND와
  record/artifact None이어야 한다. 기존 fixture/head/9trust 의미를 유지한다.
- 수정 전 HANDLE_CLOSED는 명시적으로 기록하고 자원을 닫은 뒤 의미 assertion을 실패시킨다.
  예상 실행은 **1 method / FAIL1 / ERROR0 / SKIP0**이다. 오류를 정상 응답으로 대체하지 않는다.
- 제품만 수정한 뒤 같은 test bytes·같은 입력/assertion으로 **1 method / PASS1 /
  FAIL·ERROR·SKIP0**을 확인한다. 개발용 loader 기대 hash 하나만 실제 old/fixed 파일에
  대응시키며 다른6 source와 fixture 정책·version을 바꾸지 않는다.
- 정상 응답의 실제 공개 controller read, builtin 연결 수명과 close, FD 정리, DB/tree 보존을
  관찰한다. 두 실행 모두 collector·고정 child·제품 initialize는0이며 실제 owner 승인은 없다.

수정 전 재현을 먼저 원문으로 보존하고 나서 coordinator가 제품을 수정해야 한다.
새 결과/로그·source/test 사본은 exception-context-before/after 등 별도 경로에 두고 실제
명령·cwd·환경·plan/review pin·예외 identity·정확한 reason과 수명을 남긴다. 새 test SHA가
양 실행에서 같고 제품이 예상 세 지점만 바뀌었는지 최종 review에서 대조한다.

## 기존 근거 재사용과 남은 판정

최초23 methods의 원래 결과/16 ERROR, fixture 보완30개, 기존22 method PASS 및 신뢰할 수
있는 subcase, 실제 child3회, M1/M2·원래13+3·R03/R04/R15는 그대로 보존하고 반복하지 않는다.
이 새 regression은 실제 발견된 버그의 재현이며 D38의 세 번째 변이가 아니다. 기존 오류
정리와 mutation target의 source 경계는 바뀌지 않아 원래 pin의 결과와 제한된 diff를 조합해
재사용할 수 있다. 최종 source에서 전체 suite를 실행했다고 합산하지 않는다.

새 동일 문제의 수정/한정 재검증은 D17/D18 안에서 누적하고 기존 native sandbox 보류를
재개하지 않는다. 이 계획 승인과 나중의 제품/실행 최종 독립 승인은 별개다. 운영 설치·
실제 owner 값·supervisors/issuer/서명/required-check·worker/scheduler·main은 범위 밖이다.

reviewer는 본 사전 검토 문서만 작성했다. 제품·검사·계획을 수정하거나 import·실행하지
않았고, credential/network/권한/운영 변경·추가 위임·MCP 통지를 수행하지 않았다.
