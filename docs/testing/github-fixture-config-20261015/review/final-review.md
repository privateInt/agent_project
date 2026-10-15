# L01 최종 독립 검토

판정: **PASS — 아래 고정 pin의 fixture 설정 로더·새 검사·개발 실행 근거 범위**.
미해결 제품 finding은 없다. 실제 owner 승인값·운영 설치·native 격리·검사 발행·필수 gate·
병합 승인을 뜻하지 않는다. 게시·영구 경로 보존·게시 commit readback은 coordinator의
후속 전달 단계이며 이 검토가 그 실행을 대신하지 않는다.

## 정확한 검토 pin

부모 commit은 `c91e9e628402fcc89c2daa4301d54c3c321221ed`이고, 작업 branch는
`codex/github-fixture-config-loader-20261015`다.

| 파일 | SHA-256 |
|---|---|
| `src/ops/tools/github_fixture_config.py` | `63f5faf6a0bb828928d14bc97a173388ffaca8829f7bad4621a6981e8f180e88` |
| `src/ops/tools/tests/test_github_fixture_config.py` | `bc446cb5f19d76dbe83c8900b430baa453ee6ff757c7bc21c9e7143be479d87f` |
| `docs/codex/github-fixture-config.md` | `b8a98e1de0623606afc5a2d0dc09b4b9f389a8c2a76db9a128417e543225e76e` |
| `docs/development.md` | `47f74ca4a4d14f13e225da7522309fa629f2a090a2baa0a348e0b7e88cdbbc31` |
| `docs/status.md` | `54b7caad4b7ebb32072a70008c91299fa49a1b670020f131a8644c7e11b82bfb` |
| `docs/roadmap.md` | `e72d9420c299cf6558cc03feb3b0f3d8e7c3fc1336ae46ba7644907e5cb06f86` |
| `docs/HANDOFF_NEXT_SESSION.md` | `fb9a156aeb6ee198eceef707cdd4e54142f3dbadbed446597e051d56509b598b` |
| 관찰 JSON: 최종 판정 갱신 전 | `0e98d5fc9338824bc6ead54a8fb70e38742d6a5e280d3833ee76de9205a4a04c` |

위 관찰 JSON은 `docs/testing/github-fixture-config-observed-20261015.json`이다. 현재
`final_independent_review.status=PENDING`인 사전 최종본을 읽었다. coordinator가 본 verdict의
파일·SHA를 해당 final_independent_review 객체에 반영하는 것은 이 검토 후 메타데이터 갱신이다.
그 갱신을 제품/검사의 새 실행으로 세거나 기존 결과 값을 바꾸면 안 된다.

사전 기준은 L01-D29-v1 SHA `b350192d28825de5761c448ddff6f73f00dcf778fceec164c2f2134eda353d50`,
독립 승인 `1deb9bb9673684848bf3843d79628e02cf9d42c994445c7c78829ba89ac856e4`다.
fixture 보정 v2와 exception-context v1의 사전 승인도 해당 실제 실행의 plan/review pin과
일치했다. 제품 작성자는 coordinator, 검사·계획·임시 runner 작성자는 별도 test_agent다.
reviewer는 제품·검사 작성이나 실행을 하지 않았다.

## 제품과 수정 범위

최초 전체 loader와 최종 파일·계약, 기존 audit/collector/ledger/controller API, 실제 test
전체와 변경 diff, 원본 실행 JSON/로그, 두 mutant source/로그, 임시 runner를 읽었다.
외부 version·exact7 source hash와 같은 runtime 설치·namespace를 대조하고, nofollow
경로 walk·UID/mode·전용 state·기존 서로 다른 DB inode·sidecar·header·schema를 확인한
뒤 기존 controller로 직접 전달하는 구조다. 제품의 자동 mkdir/chmod/init/migrate,
동적 import/exec·새 subprocess·network·재시도·재수집은 없다.

첫 중간 finding의 빈 temp namespace 거부는 `_connected_inode`가 유일 main을 골라
inode를 확인하도록 보정됐다. 기존 API가 허용하는 빈 temp만 허용하고 추가 attached DB나
temp 객체는 원래 검증이 계속 거부한다. 이 보정은 첫 실제 실행 전 source06c1→487d의
정적 수정이며, 실제 첫 검사 실패를 재현한 것으로 소급하지 않는다.

현재 source63f5와 첫 실행 source487d의 차이는 승인된 로컬 성공 flag 세 지점뿐이다.
원래 source bytes를 메모리에서 이 세 지점만 바꿨을 때 최종 파일과 정확히 같았다.
False는 자원 구간 시작에서 설정하고 마지막 `_check()` 뒤에만 True가 된다. 기존 실패의
cleanup 본문·except·guard·SQL·reason과 다른 정의는 그대로다. caller except의 문맥 때문에
정상 반환 handle을 닫던 실제 결함을 해결하며, 정상 로컬 실패 정리는 유지한다.

검사 변경도 원본2ef1→a2e3의 altered finally 한 줄, 그 뒤 bc446의 새 exception-context
helper·dispatch·등록 추가뿐이다. 수정 전/후 보관된 bc446 검사 bytes와 현재 검사 파일이
같음을 확인했다. 처음의 생성 전 guard를 잘못 가리킨 fixture 보정 v1과 REQUEST_CHANGES,
메타데이터 대조 오류는 보존됐고 그 잘못된 보정은 실행되지 않았다.

## 실제 실행 근거와 귀속

이번 서버의 Python3.10.12·SQLite3.37.2·euid1011·namespace root/tmp UID65534 근거다.
이전 Python3.12.14·SQLite3.53.1 개발 결과를 이 실행으로 합산하지 않았다.

| 실행 | 적용 pin과 실제 결과 |
|---|---|
| `evidence/run-1` | source487d/test2ef1. 최초23 methods 중22 PASS, FAIL0/ERROR16/SKIP0. subcase264개 중 PASS248/ERROR16. test_agent 실행 |
| `evidence/fixture-retry-1` | source487d/testa2e3. 두 wrappers, L09의 가려졌던14개와 L17의 미도달 read16개만 PASS30, FAIL/ERROR/SKIP0, child0. test_agent 실행 |
| `evidence/exception-context-before` | source487d/testbc446. 새 target1의 의미상 FAIL1/ERROR0/SKIP0. 실제 caller except 안에서 공개 read가 HANDLE_CLOSED로 거부된 최초 재현. test_agent 실행 |
| `evidence/exception-context-after` | source63f5/동일 testbc446. 같은 target1 PASS, FAIL/ERROR/SKIP0. coordinator가 변경 없는 test_agent runner로 실행 |

최초16 ERROR는 run-sidecar 뒤 테스트 helper가 남긴 파일 때문에 read의 목표 변경 주입
전에 load가 올바르게 거부한 오류다. 원문을 지우지 않았고, 해당16개의 새 통과 근거는
별도로 연결한다. L09의 첫 journal empty/link 두 개는 원래 독립 PASS를 재사용하고 뒤14개는
기존 PASS 표기를 보존하면서 새 독립 근거로 보완했다. L05의 남은 namespace initializer는
현재 제품 순서상 namespace-path 거부 뒤에 검사되므로 기존 실행·정적 근거를 재사용했다.

새 exception-context 검사에서 caller RuntimeError identity는 전후 세 번 유지된다. 수정 전
HANDLE_CLOSED를 명시 assertion FAIL로 검출했고, 수정 후 실제 JOB_NOT_FOUND와 None
record/artifact, 열린 builtin 연결2개 및 명시 close 뒤 정리를 확인했다. fixture tree와
DB가 보존되고 양 실행 모두 collector/child/제품 init0이다. 예외를 허용하도록 기대값을
바꾼 것이 아니다. stdlib-only 관찰은 이 실제 제품 재현과 별개로 보존했다.

고정 child는 최초 실행의 ok/exit0, nonzero/exit7, 실제 기록 뒤 응답 유실 주입의 ok로
정확히3회다. 저장 후 재open에서 원래 artifact bytes/hash/binding7과 행을 확인했다.
exit7을 실행 성공으로 승격하지 않는다. 최초 성공 child나 기존13+3·R03/R04/R15·전체
suite는 반복하지 않았다. 최종 source에서 전체24 methods를 실행한 결과로 합산하지 않는다.

## 계획과 근거 대응, L17/L18 동등성 판단

| 계획 범위 | 실제 근거와 한계 |
|---|---|
| L01~L07 | strict config/기대값/marker/source pin/runtime origin·namespace/조상/state 허용·거부. UID 오류 일부는 명시 fstat metadata 주입이며 실제 다른 UID 생성은 아님 |
| L08~L10 | 두 DB 경로·mode·link·누락/alias, sidecar, schema·application ID/version, 실제 closed-WAL/no-sidecar header 거부. sidecar14개는 새 한정 근거 사용 |
| L11~L12 | 실제 builtin 연결과 URI 문자 인코딩·FULL/busy 설정, close/실패 정리 및 위 새 caller-except regression. 과거 오류 정리는 원래 결과와 동일 cleanup의 제어 흐름 대조로 재사용 |
| L13~L16 | 실제 fixture→기존 두 DB→재조회/재open, 확정 준비 실패의 영속성, 합성 불명·실제 commit 뒤 응답 오류 주입, 반환 identity stub과 실제 두 state 라우팅을 구분 |
| L17 | run16개는 최초 PASS, read16개는 한정 재검증 PASS. 실제 seven source bytes·marker version·state/ledger mode·installation/state/두 DB inode·header·sidecar 변경 거부 |
| L18 | 실제 ledger entry의 동일 bytes 새 inode 교체를 connect 직전/직후 주입한2개, state rename→symlink를 같은 두 시점에 주입한2개. 모두 거부와 자원 정리·업무 API/child0 |
| L19~L20 | 기존 정적 AST3.10·report/오류·비노출과 보존 근거. 실제 게시/readback과 최종 영구 보존은 전달 담당 후속 |

다음 두 조합은 별도로 직접 실행했다고 표시하지 않는다. 공통 검증의 동일 범위·판정을
입증한 재사용으로 D06/D07의 사소한 동등성 판단을 수용하며, 기대 결과를 완화하거나
필수→선택/SKIP으로 바꾸지 않는다.

1. **L17의 post-load origin/namespace:** L05의 각 module file/origin과 namespace-path 거부를
   재사용한다. 현재 run/read는 모두 `_check()`를 업무 호출 전에 실행하고, `_check()`는
   저장된 root/version/pin으로 같은 `_installation()`을 무조건 호출한다. 그 함수의 첫
   호출이 바로 같은 `_runtime_paths(root)`다. L17의 source/marker 변경은 두 업무 진입점이
   이 공통 설치 검증에 실제 도달함을 확인했다. origin/namespace를 별도로 저장·캐시하거나
   run/read에서 생략하는 다른 분기가 없다. 두 guard의 결과·출처·진입점이 같아 추가 실행이
   현재 검증 범위를 넓히지 않는다. 전 조합을 직접 주입한 raw 실행 근거로 소급하지 않는다.
2. **L18의 일반 state inode 교체:** 연결 창에서 직접 실행한 state 사례는 symlink 교체다.
   일반 동일 bytes state inode 교체의 거부는 L17의 run/read 실제 사례로 확인했고, connect
   전후에도 같은 `_check(schemas=False)`가 directory walk와 identity 비교를 동일하게 수행한다.
   schemas=False가 생략하는 것은 그 뒤 schema 검사뿐이다. L18의 DB inode와 state symlink
   사례는 연결 전후 검증·실패 정리 시점도 확인한다. 이 공통 분기들의 조합으로 같은 거부
   요구를 검증하며, 연결 창의 일반 state inode 교체를 직접 실행했다고 표시하지 않는다.

검증 지점 이후 같은 UID 악성 mutation을 전면 방어한다는 주장은 위 동등성 판단에 포함되지
않는다. 테스트가 주입한 결정적 race와 실제 동시 적대자/native 경계를 혼합하지 않는다.

## D38과 원본 보존

선정 변이는 정확히2개다. M1은 외부 audit hash equality 생략, M2는 state0700 검사 생략이다.
각 실제 mutant bytes가 원래 source의 해당 조건 하나만 바꾼 결과와 일치했고, 목표 검사
로그는 각각 assertion FAIL1/ERROR0/SKIP0이다. mutant loader 자신에 대한 개발 pin 하나만
변이 bytes에 대응했고, 잘못된 audit 기대 pin/state0755는 유지했다. 최초 정상 target PASS를
재사용했으며 다른 source/test 오류로 실패시킨 결과가 아니다. 이 두 guard와 target은 두
후속 보정으로 변하지 않아 원래 pin의 결과를 재사용한다. exception-context는 실제 버그
회귀 재현이며 세 번째 변이가 아니다.

test-agent-final의 retained evidence28개 해시를 실제 파일과 대조해 모두 일치했다.
최초 결과/로그·변이·fixture 보정·새 재현/통과·계획/승인 원문이 보존돼 있다. 기존 여섯
source의 실제 SHA도 고정 값과 일치했고 staged 변경은 loader/test/계약/관찰 JSON 추가와
개발·상태·로드맵·인수인계의 작은 연결뿐이다. 운영 설정이나 기존 부품 변경은 없다.

원래 checkout11개·과거 근거 보존은 coordinator의 `evidence/preservation.json` 보고에
귀속한다. 그 보고 SHA는 `828b4ce6fffc0645b51f6cfd4e51a8d738406e4312ff821c1df29c395eb0a039`다.
이 제한된 검토에서 원래 checkout·과거 archive 전체를 다시 검사했다고 주장하지 않는다.
문서 patch의 최초 atomic 거부와 검토 메타데이터/계획 정정 기록도 제품 실패와 구분한다.

## 미검증 범위와 검토자 행위

실제 owner가 승인한 version→hash/UID/경로 출처, 운영 설치 인수, 실제 다른 OS 사용자
격리, 신뢰한 Python launcher/process/pyc/stdlib attestation, DB 영구 pairing·rollback 방지,
독립 supervisors·issuer/서명·최신 binding/required-check 발행은 NOT_RUN 또는 미인증이다.
fixture_only=true, head_code_executed=false, owner_approval_verified=false와 아홉 신뢰/
병합 flag=false를 유지한다. 운영 원본·DB·Docker·credential·worker/scheduler·보호 설정·
main 및 기존 native sandbox 조사 소진 보류는 이번 범위에서 변경하지 않는다.

최종 reviewer는 코드·검사·계획/실행 원문을 수정하지 않았고 제품/검사/변이를 실행하지
않았다. 본 최종 검토 파일만 새로 작성했다. 읽기 전용 source/metadata/hash 대조를 했으며
credential/network/권한/운영 접근, 추가 위임·MCP 통지는 하지 않았다.
