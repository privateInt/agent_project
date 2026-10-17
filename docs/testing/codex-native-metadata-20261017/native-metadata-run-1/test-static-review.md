# Native metadata 새 검사·fixture 정적 검토

판정: **PASS** — 아래 exact 검사/fixture의 정적 요구 대조에 한정한다. runner/실행 승인·실제 검사 PASS·native 인수 판정은 아니다.

- test SHA256: `e6c485f1e1852ca365c70cdc5986478fee3f8619182dd00a19b8ed1d1589d3ac` (19,460 bytes)
- manifest.json SHA256: `007d17ec97c30eec9f5d5b30f9d9205e500505ec3f3626f54062f4a81f154862`
- synthetic.json SHA256: `22036ceaf79a83def772b42110ef076343207a3c296c162d7df623efdea287b6`
- transport_primitives.py.txt SHA256: `44639fbdfd9d120e82abd158f4b8a9416ba1122a3d6184014825233a1564fc0d`
- primitive-extraction-audit.json SHA256: `9d76bdae81125a2840cde0d542b68d3f6583322e43f47098433a111edcd3e3cf`
- dependency-pins.json SHA256: `b92969d34fd6945205015ec3bcee29dd04001d475eff65a5f18cce872e96ee13`

**새4의 요구 행동:** wire method(174–211행)는 caller initialize/initialized 뒤 helper read1회와 별도 turns1회의 exact params/고유 ID 및 decoded result 전체 보존을 확인한다. nextCursor가 있어도 추가 request가 없고 False/default에서는 metadata가 거부된다. 차단 method(213–258행)는 금지 method, 단일 thread/type/exact keys, cursor/order/view/limit, 빈·복수 thread 생성자 거부를 다룬다. 거부 시 frame/ID 소모0과 생성자 connect0을 확인하며 잘못된 helper 호출은 자원 close도 확인한다. cursor=null/limit1/notLoaded/desc의 synthetic params는 승인된 5-key 계약과 맞는다.

wrong-id method(260–295행)는 두 RPC의 잘못된 JSONRPC ID, helper의 missing/malformed/다른 thread.id, ProbeError 및 일반 call 오류를 분리한다. helper의 fixed code·원문 오류 미포함·ProbeError 동일 객체 전달, 실제 fake socket close/ws=None/sock=None 및 자동 후속0을 단언한다. direct turns의 close는 caller가 명시한다. deadline/loss method(297–332행)는 기존 실제 send 뒤 request/total 경계 도달, send/recv 실패, 송신 전 total 만료를 주입해 fixed code·request≤1·한 연결·close를 확인한다. 유실을 확정 미송신이나 원격 완료로 바꾸지 않는다.

**실제 전송 fixture/기존 TestCase 실행0:** 새 ReadOnlyMetadataTests만 unittest.TestCase를 상속한다. primitive 파일에는 이전 TestCase/setup/classmethod/business fixture가 없으며 module-level encoding/frame/response 함수와 TransportFixture만 있다. 새 StrictFixture는 이 module-level fixture만 상속한다. 추출 TransportFixture 본문은 고정 기존 파일의 해당 범위와 실제 text diff가 없음을 확인했다. SOURCE는 새 workspace 상대 경로로 지정하고, fake socket superclass에서 exact 제품 source를 로드한다. websocket-client 1.9.0의 실제 handshake/frame encode/decode와 제품 call/ID 대조를 사용하며 outgoing_frame은 fake가 캡처한 송신만 읽는다. 제품 알고리즘을 별도 대역으로 복제하지 않는다. 실제 기존 TestCase method를 실행·상속하는 경로는 없다.

**두 semantic target의 정적 적합성:** case_read_only_include_turns_true_never_sent와 case_read_only_limit_two_never_sent는 승인된 exact params에서 해당 값만 True/2로 바꾼다. 정상 target은 두 번째 method 안에서 각각1회 호출되며 send0/ID 미소모/PARAMS를 요구한다. _blocked_mutation_target(140–164행)은 실제 요청·wire bytes/hash/base64·next_id를 assertion 전에 기록한다. 해당 guard만 제거된 변이에서는 잘못된 params가 실제 fake wire에 쓰인 뒤 지정 fixed-code assertion에서 의미 FAIL이 날 구조다. 단순 import/다른 guard ERROR를 검출 성공으로 보지 않는다. 실제 mutant AST의 단일 변경, target 각각1회, actual wrong wire 일치, FAIL1/ERROR0 gate는 아직 읽지 않은 runner의 후속 정적 검토와 실행 근거로 확인해야 한다.

**fixture pin/격리 경계:** manifest의 두 파일 hash는 실제 bytes와 일치한다. synthetic endpoint/UUID/private marker만 사용하고 fixture provenance도 synthetic-only다. 추출 audit는 기존 test pin `5085ce3d…`의 여섯 primitive와 AST 동등성·old TestCase 미추출을 기록한다. dependency 기록은 기존 websocket-client 1.9.0 파일 pin 목록이며 외부 설치/환경을 추가 조회하거나 import해 검증하지 않았다. fresh source copy의 실제 dependency pin/경로 독립성은 runner 고정 시 대조해야 한다.

fixture는 real socket/create_connection/DNS/socketpair/Popen을 BaseException 금지 경로로 막고 thread.start/sleep도 금지한다. stdout/stderr와 graceful close/재연결 시도를 감시하며 마지막 owned fake 자원을 정리한다. 파일/credential/운영 접근까지 막는 전체 IO guard, source/test/fixture/plan/review pin, 정확4+2 선택은 미고정 runner의 별도 필수 gate다. 이번에는 run-once.py나 tests-ready를 읽거나 실행 승인하지 않았다.

미해결 검사/fixture 정적 finding은 없다. 이 판정은 위 pin에만 적용하며 이후 bytes 변경 시 해당 delta의 새 pin 검토가 필요하다. SOURCE/PACKAGING 검토는 재사용했고 반복하지 않았다. 제품/검사/fixture import·실행·기존 suite·native/RPC/socket/다른 agent 조회0이며 이 보고서만 작성했다. 원본11·기존 결과·CLOSED 조사·scheduler PAUSE를 유지하고04:00Z 단위 마감을 연장/재기산하지 않는다.
