# Native metadata client D29 독립 사전 검토

판정: **PASS** — 아래 원본 계획과 보정안, 이번 사용자 명시 조정을 결합한 계약에 대한 구현 전 판정이다. 원본의 생략형 turns 계약만 단독 적용해서는 안 된다.

- 원본 계획 SHA256: `67953b3166ad22a3211a85cd701511811aaffe71188f6291d7d3c811be74aa8c`
- 보정안 native-metadata-plan-addendum.md SHA256: `d101e3a675c01155feaa6ce07649d1cf8dab4faadb9b6af03469a455cd214394`
- 재사용 PR16 transport SHA256: `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415`
- 기준 PR22 commit: `d59de21d4b43a71f3e518592c589b0761e668de5`; 별도 native-metadata-development에서만 구현한다.
- 로컬 단위 마감: **2026-10-17 04:00:00Z**, 연장/재기산 없음. native 조사는 CLOSED이며 추가 조회0이다.

**구현에 적용할 정확한 보정:** read_only turns의 exact keys는 `{threadId, cursor, limit, itemsView, sortDirection}`이고, 값은 단일 허용 thread / null / exact builtin int 1 / builtin str notLoaded / builtin str desc다. cursor와 sortDirection의 생략·다른 값·추가 key는 PARAMS 거부/send0이다. 원본 계획의 3-key·cursor 생략 문장을 이 계약으로 대체한다. 제한된 최신 첫 페이지일 뿐 전체 history를 조회한 것으로 표시하지 않는다.

같은 UnixWebSocketRpc에 `read_thread_metadata(thread_id)`를 추가한다. 명시 read_only=True와 단일 허용 thread를 확인하고, 기존 `call('thread/read', {'threadId': thread_id, 'includeTurns': False})`를 정확히1회 사용한다. decoded result의 thread가 dict이고 id가 요청 ID와 같은지 확인한 뒤 기존 decoded result 전체를 반환한다. 자동 initialize/initialized/turns/pagination/retry/reconnect는0이다. initialize와 notification은 caller가 명시 수행한다. helper의 모드·입력 거부, call 오류, 누락/비정상 thread 또는 다른 thread.id는 모두 close 후 고정 ProbeError로 실패하며 원문을 오류 메시지에 넣지 않는다. 기존 call의 고정 ProbeError는 close 후 그대로 전달하고, thread 응답 불일치의 고정 code는 SOURCE_READY에 명시해 검사와 일치시킨다.

decoded result는 비식별화된 새 report가 아니다. caller에게 반환되는 원문에 임의 metadata/body가 포함될 수 있으며 제품이 이를 저장·로그·전역 cache로 남기지 않는다. v3 관측기의 enum/hash projection을 이 helper가 수행한다고 주장하지 않는다. 반환으로 ready·권한·사용량·owner/native 인수를 승격하지 않는다. 이것은 기존 client에서 실제 전송 경로를 호출하는 편의 진입점이며 배포된 controller가 이미 호출한다는 증거가 아니다.

**D06 분류:** turns 보정은 사용자 명시 지시에 따라 이미 관측한 exact params로 한정하는 사전 계약 정정이다. 파라미터 생략형을 호환 계약으로 새로 가정하지 않는다. helper 추가는 사용자가 명시한 좁은 범위 추가이며 기존 보호·판정의 완화가 아니다. 아직 제품/검사 작성·실행 전이고 기존 PASS 기대값을 낮추지 않는다. 나머지 원본 요구를 유지하며 위 결합 계약과 두 계획 pin을 source/test/runner 근거에 함께 연결해야 한다.

**새 정상4 methods의 필수 범위:** wire method에서 caller initialize/initialized 뒤 helper1회와 별도 turns call1회의 정확한 wire params/고유 JSONRPC ID/decoded result 및 자동 후속0을 확인한다. default/False의 기존 METHODS를 유지하고 metadata는 True에서만 허용한다. 차단 matrix에는 5-key 계약·타 thread·builtin 타입·모드·단일 thread·helper의 False 거부를 포함하고, 거부 시 request frame/ID 소모0 및 생성자 거부 시 connect0을 확인한다. wrong-id method는 두 명시 RPC의 JSONRPC ID 거부에 더해 matching JSONRPC ID이나 다른 thread.id인 helper 결과를 거부하고, helper의 call 오류·응답 검증 오류 뒤 실제 fake 자원 close 및 후속 송신0을 확인한다. deadline/loss method는 기존 clock/전송 경로와 request/total 경계·송신 뒤 유실을 사용해 재전송/재접속/자동 후속0을 입증한다. helper가 오류를 삼키거나 잘못된 응답을 반환하면 실패해야 한다. 추가 TestCase method는 요구하지 않는다.

**지정 semantic mutants2:** M_INCLUDE_TURNS와 M_LIMIT는 이번 두 strict guard에 적합하다. exact 5-key params의 나머지 thread/shape/type/order/view 검사를 유지하면서 각각 단일 predicate만 True로 치환한다. 지정 helper 정상 PASS를 차단 method에서 각1회 얻어 재사용하고, 정상4 전부 PASS 뒤 mutant별 지정 helper만1회 실행한다. 실제 fake wire의 잘못된 includeTurns=True 또는 limit=2 request/ID witness와 지정 의미 FAIL1/ERROR0이 필수다. 조기 예외·import 오류·다른 AST 변경은 검출 성공으로 인정하지 않는다. helper의 ID/close 검증은 위 정상 wrong-id method에서 직접 단언하므로 이 단위에 별도 mutant나 기존 suite 반복을 추가할 필요는 없다.

**재사용·격리·시간:** 기존 METHODS/default 동작과 전송/decoder/한계/ID/deadline 코드는 보존하며 별도 dispatcher/DB/가상 module 제품을 만들지 않는다. 원본 transport bytes와 반입 diff를 구분하고 새 relative fixture/fresh source copy 및 실제 encode/decode를 통과하는 fake socket/clock을 사용한다. IO guard는 제품이 잡는 Exception으로 소실되지 않도록 BaseException으로 금지 시도를 실패시켜야 한다. old TestCase/setup/classmethod 실행·상속 없이 primitive만 재사용한다. 기존 정상4/구 suite를 재실행하지 않는다.

isolation에 기록된 시작은 03:26:02.516985Z, 고정 마감은04:00:00Z다. 03:28:32Z 검토 시점 기준 남은 약31분 안에 이 좁은 구현·새4/변이2·독립 검토·publication/readback을 수행하는 계획은 적절하다. 먼저 수행한 같은 로컬 단위의 분석 시간을 포함하고 실패 수정 최대3회/누적 시간 제한을 이어받는다. 원래 종료선 안에 완료하지 못하면 실제 상태·잔여 작업을 checkpoint하고 중단하며 PASS나 완료를 가정하지 않는다. CLOSED native 창의 잔여 시간/RPC는 이월하지 않는다.

최종 source/test/fixture/원본 diff 고정 및 정적 대조 이후에만 새 검사를 실행한다. 최초 실패와 통과 결과는 보존하고 변경된 실패 target만 재검증한다. 이 사전 PASS는 구현/실행의 최종 독립 검토를 대신하지 않는다. 실제 수정본 owner-client/controller/native 연결과 두 native 작업은 NOT_RUN으로 남기며, 고정 v3 7RPC 관측을 새 제품 인수로 재사용하지 않는다. 후속 native 배포·실행에는 별도 승인·소스/endpoint/thread/권한 pin이 필요하다.

이번에는 계획·보정안·격리 기록·관련 기존 source/fake primitive와 D29/D06 지침만 정적으로 읽고 이 보고서만 작성했다. 제품/검사 편집·import/실행·추가 환경/socket/RPC/다른 agent 조회는 없다. 원본11·과거 PASS/최초 오류·receipt·scheduler/root/user PAUSE·운영/auth 보호를 유지한다.
