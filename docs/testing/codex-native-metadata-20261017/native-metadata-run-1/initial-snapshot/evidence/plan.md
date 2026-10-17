# D29 — 기존 UnixWebSocketRpc의 고정 thread metadata read_only 지원
상태: PLAN_READY / 독립 사전 검토 전. 이번 산출물은 계획뿐이며 제품 수정·import·검사·추가 설치조사·RPC는 수행하지 않는다.
기준 PR22: `d59de21d4b43a71f3e518592c589b0761e668de5`.
별도 작업공간: `ROOT/native-metadata-development`; 기존 checkout/미커밋 변경/과거 PASS·최초 오류는 보존한다.
ROOT=`/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015`.
재사용 transport 원본: `ROOT/bridge-development/src/ops/tools/codex_daemon_websocket.py`.
원본 SHA256=`7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415` (별도 PR16 검토선).
PR22에 별도 transport 파일을 반입할 때 위 원본 bytes를 먼저 보존·pin하고, 그 원본과 이번 변경 diff를 분리한다. 다른 PR16 사업 로직/운영 script는 반입하지 않는다.

## 목적·경계
기존 controller/caller가 소유한 기존 socket endpoint에 기존 client로 제한된 metadata를 요청할 수 있게 하는 실제 transport 준비 단위다.
새 client 종류/dispatcher/가상 런타임/DB/schema/worker/service/owner 설정은 만들지 않는다. endpoint·실제 기존 thread ID는 caller가 제공한다.
실제 owner-client 선택/transport source pin/사용량·권한/설치·배포 승인은 외부 책임이며 입력값만으로 검증됐다고 표시하지 않는다.
이번 현장 확인 `observe_metadata_once_v3`와 그 03:31:41Z 종료는 별도다. 이 구현 계획으로 D18 조사/RPC 예산·시간을 연장·재설정하거나 현장 확인을 반복하지 않는다.
root/user/scheduler PAUSE·운영 원본·DB·인증정보·VMS 보호를 유지한다. 본 계획 작성과 이후 구현 인수에서는 native 호출0이다.

## 변경 파일
제품: `src/ops/tools/codex_daemon_websocket.py` 한 개. 고정 PR16 전송/decoder/ID/deadline/close 경로를 재사용한다.
새 검사: `src/ops/tools/tests/test_codex_daemon_websocket_read_only.py` — 아래 새 4 methods만.
상대 synthetic fixture: `src/ops/tools/tests/fixtures/codex_daemon_websocket_read_only/synthetic.json` 및 hash manifest.
향후 실행 runner/snapshot/log/result: `ROOT/transition-evidence/native-metadata-run-1/`; 기존 실행·검사 산출물과 분리한다.
기존 `test_codex_daemon_websocket.py`의 4 methods와 fixture/operator script·다른 제품 모듈은 수정·실행하지 않는다.

## 공개 API·정확한 허용 입력
`UnixWebSocketRpc(endpoint, allowed_threads, *, total_timeout=60, request_timeout=5, read_only=False)`.
`context/call/notify_initialized/close`와 decoded result 반환, `ProbeError.code` 계약은 유지한다. 새 공개 결과/report/설정 파일은 없다.
read_only는 exact builtin bool만 허용한다. 명시 모드가 True면 기존 검증을 통과한 allowed_threads가 정확히 한 기존 thread만 포함해야 한다.
False/default에서는 PR16의 기존 method/params 허용 계약을 보존한다. 새 metadata 허용 경로는 True에만 추가하며 기본 권한을 넓히지 않는다.
True에서 `call`이 허용하는 method는 기존 `initialize`, `thread/read`, `thread/turns/list`뿐이다.
`notify_initialized()`의 기존 초기화 notification만 유지한다. 다른 notification/call 경로로 method 제한을 우회할 수 없어야 한다.
`thread/read` params의 exact keys는 `{threadId, includeTurns}`; threadId는 고정 allowlist의 단일 ID, includeTurns는 builtin False만이다.
`thread/turns/list` params의 exact keys는 `{threadId, itemsView, limit}`; 동일 threadId, itemsView=builtin str `notLoaded`, limit=exact builtin int 1만이다.
첫 페이지는 cursor 생략으로 고정한다. cursor는 null/빈 문자열도 포함해 추가 key로 거부하고 sortDirection 등 다른 추가 key도 거부한다.
단순 spelling `turns/list`는 API alias로 추가하지 않는다. 실제 wire method는 `thread/turns/list`다.
resume/settings 변경/queue add·list/account/config/new thread 및 임의 method는 `RPC_METHOD_FORBIDDEN`으로 연결·송신 전 로컬 거부한다.
허용 metadata method의 다른 thread/추가·누락 key/includeTurns True·0·문자열/itemsView 변경/limit True·1.0·0·2/페이지 입력은 `RPC_PARAMS_FORBIDDEN`이다.
비-bool mode/True의 빈·복수 thread 집합도 `RPC_PARAMS_FORBIDDEN`으로 첫 연결 전 거부한다. 기존 initialize params 검증은 그대로다.
검사는 기존 전송기가 할 수 있는 최소 선검사 지점에서 수행한다. client가 이미 열려 있어도 거부된 call은 metadata request frame을 전혀 쓰지 않는다.

## 전송·ID·실패 계약
허용 call은 새 대체 decoder/callback이 아니라 실제 기존 call→JSONRPC request ID→WebSocket encode/send→decode→ID 대조→decoded result 경로를 사용한다.
요청 1회당 JSONRPC request/frame 1개·고유 ID 1개만 보낸다. send의 부분 write 횟수를 요청 재전송 횟수로 혼동하지 않는다.
read와 turns는 별도 명시 call이며 client가 두 번째 call을 자동 실행하지 않는다. 서버 nextCursor가 있어도 follow-up/pagination을 생성하지 않는다.
다른 response ID를 현재 요청의 결과로 반환하지 않는다. 고정 전송기의 `RPC_RESPONSE_ID_MISMATCH` 거부 계약을 새 metadata 경로에도 적용한다.
request_timeout/total_timeout은 기존 deadline 계산을 공유하고 `RPC_REQUEST_TIMEOUT`으로 닫힌다. 새 clock/sleep/backoff/retry/재접속은 추가하지 않는다.
timeout/응답 유실/close 뒤에 replay·다음 metadata 요청·resume/설정 변경을 자동 생성하지 않는다. call entered는 wire 전송·native 수락 증명이 아니다.
원문 thread/turn payload를 새 로그/DB/global cache에 남기지 않는다. 기존 decoder의 size/finite/ID guard를 약화하지 않는다.

## 새 검사 정확 4 methods — fake socket/clock만, 실제 전송 코드 경유
`test_read_only_metadata_wire_and_id_binding`: 고정 synthetic thread·fake socket의 실제 encode/decode를 사용한다.
초기화+initialized 기존 순서와 두 허용 metadata의 wire method/params/고유 ID를 직접 대조하고 matching ID의 decoded result만 반환되는지 확인한다.
각 metadata call request frame 1개, 다음 cursor 응답에도 자동 후속 request0을 확인한다. default/False의 기존 mode 선택이 유지되고 True만 새 경로를 여는 것도 같은 method에서 확인한다.
이 검사는 허용 목록 선언만 존재하고 실제 전송 경로가 연결되지 않은 구현을 배제한다. 기존 PASS를 다시 실행하는 검사가 아니다.
`test_read_only_blocks_mutation_thread_and_pagination`: 위 금지 method·params/type/다른 thread·mode/단일 thread 요건을 matrix로 검사한다.
호출 전후 fake socket에 새 request bytes0·request ID 소모/재시도0, 고정 error code를 확인한다. 생성자 오류는 connect0이다.
이 method 안에서 아래 helper 둘의 정상 PASS를 각1회만 기록한다. 새 mode의 권한 제한과 first-page/작은 응답 계약을 검증한다.
`test_read_only_wrong_response_id_is_not_accepted`: 두 metadata method에 각 matching ID와 불일치 ID의 분리된 fake 응답을 주고 실제 기존 decoder/ID 대조를 확인한다.
불일치에서 고정 error, 잘못된 result 반환0, 해당 call request1/재전송0·자동 후속 call0을 확인한다. 과거 ID 검사를 통째로 반복하지 않는다.
`test_read_only_deadlines_and_loss_never_replay`: 기존 clock seam과 fake socket으로 request/total deadline 경계 및 송신 뒤 응답 유실을 주입한다.
timeout 고정 code와 각 call request≤1/재시도0/재접속0/추가 method0을 확인한다. 유실을 완료·확정 미송신으로 판정하지 않는다.
이는 새 metadata 분기가 deadline을 우회하거나 재전송하는 회귀만 검사하며 실제 시간 대기/native 연결은 없다.
모든 검사는 BaseException IO guard로 실제 socket connect/네트워크/subprocess/credential/환경변경/운영 파일 접근을 차단한다. fake socket은 주입한 기존 seam만 사용한다.
기존 module-level fake/primitive만 pin해 재사용할 수 있으나 old TestCase method/setup/classmethod를 호출·상속하지 않는다. 새 component나 대역 제품 알고리즘을 만들지 않는다.

## 최소 semantic mutants 정확 2개
M_INCLUDE_TURNS: read_only read의 단일 `includeTurns is False` predicate만 True로 치환한다. 다른 shape/type/thread 검사는 유지한다.
target helper `case_read_only_include_turns_true_never_sent`: 고정 thread·유효 shape의 includeTurns=True를 제시하고 정상은 PARAMS 거부/send0이어야 한다.
M_LIMIT: read_only turns의 단일 `limit == 1` predicate만 True로 치환한다. exact int/type/shape/thread/notLoaded 검사는 유지한다.
target helper `case_read_only_limit_two_never_sent`: 유효 다른 필드·limit=2를 제시하고 정상은 PARAMS 거부/send0이어야 한다.
각 mutant는 실제 fake wire에 잘못된 params가 쓰인 request frame/ID witness가 있어야 지정 의미 assertion FAIL1/ERROR0이다. import/다른 guard/fixture 오류는 검출 성공이 아니다.
정상 helper PASS는 두 번째 정상 method에서 각1회만 얻어 재사용한다. 정상4 전부 PASS 뒤 각 isolated mutant에서 지정 helper만1회; 나머지 AST/원본 source는 동일해야 한다.
새 두 params 제한을 우회해도 잘못 송신하지 않는다면 mutation target 설계를 독립 검토에 돌리고, 실행 뒤 기대 기준을 낮춰 통과시키지 않는다.

## 실행 gate·완료선
독립 D29 prereview PASS 전 제품/검사를 작성하지 않는다. 이후 고정 PR22 별도 workspace와 원본 transport pin을 확인·보존하고 제품/새 검사를 병행 작성한다.
SOURCE_READY는 변경 source SHA·원본 diff·새 guard의 정확 AST 위치를 고정한다. 독립 source review와 test/fixture/runner 정적 대조 PASS 후에만 실행한다.
실행 runner는 source/test/fixture/plan/사전·source review/environment pin을 확인하고 fresh 최소 source copy에서 Python3.10 호환 `-I -S -B`로 새4 failfast 최초1회만 실행한다.
정상 FAIL/ERROR/SKIP이면 최초 raw error·source/test/fixture/log/result를 보존하고 중단한다. 독립 판단 후 원인이 있는 해당 target만 수정·재검증하며 정상 PASS·old suite는 반복하지 않는다.
정상4 PASS 후 지정 mutant helper2를 각1회 수행하고 actual wrong wire witness와 FAIL1/ERROR0를 보존한다. 실제 native/socket/CLI/worker/RPC 인수는 이 실행에 포함하지 않는다.
최종 독립 review→이번 단위만 commit→PR22 대상 Draft PR→게시 source/test/근거 exact hash readback을 완료선으로 한다. main 병합·설치·서비스 시작은 별도 승인 범위다.
성공 표기는 고정 thread read_only의 실제 전송 경로/ID 대조/차단/1회 요청/deadline 인수로 한정한다. worker 활성화·전체 controller 완료·native 배포 인수로 확대하지 않는다.

## 설치 제안과 구현 인수의 구분 — 기존 결과만 재사용
`native-contract-20261017/installation-comparison.json` SHA=`cc9459eb2ebd3992abb9c6672b322dce742c36c4bb1d905dc576cfbba6a53427`을 재사용하고 추가 현장조사는 하지 않는다.
기존 service는 workspace/automation_mvp·never/ephemeral, console은 VMS 포함이다. 새 loader config/실제 역할 thread/owner 승인은 지정 범위에서 미확인이며 여기서 생성하지 않는다.
기존 fixture loader의 SOURCE_FILES 7개에는 transport와 unattended 부품이 없다. 이 변경만으로 설치 pin 적용·UID1011 적합·실제 owner socket 권한이 확인되는 것은 아니다.
후속 설치 담당자는 별도 agent_project 전용 staged 경로·transport pin·caller-owned endpoint/단일 기존 thread·read_only=True를 명시한 launcher안을 검토해야 한다.
실제 승인값·thread/client 신뢰는 owner가 제공하며 서비스/콘솔/DB/PAUSE 변경은 지금 실행하지 않는다. 기존 경로·hash·운영 원본을 보존하고 별도 배포/복귀 승인 전 PAUSE를 유지한다.
