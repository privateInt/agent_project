# Native metadata client 최초 소스 정적 검토

판정: **PASS** — 아래 갱신된 exact source pin의 정적 계약 대조 결과다. 검사 실행·최종 구현/native 인수 PASS가 아니다.

- 검토 source SHA256: `6f704fc2d308406eb27e614e88cfb0bb4c317f8f6c695afa48319bdc5d402432`
- 보존 PR16 원본 SHA256: `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415`
- 원본 계획 SHA256: `67953b3166ad22a3211a85cd701511811aaffe71188f6291d7d3c811be74aa8c`
- 보정안 SHA256: `d101e3a675c01155feaa6ce07649d1cf8dab4faadb9b6af03469a455cd214394`
- 기준 PR22: `d59de21d4b43a71f3e518592c589b0761e668de5`; 단위 마감 **2026-10-17 04:00:00Z**, 연장/재기산 없음.

**판독 중 갱신과 최초 finding 보존:** 처음 읽은 source/source-ready pin은 `0b706592dd1b76aa9f7044f2713b4fdada6d9e4d25887436e7fb0ef610ac422a`였다. 이 판독본에는 (F1) 빈 allowed_threads/read_only=True가 원본 nonempty 검사에서 RPC_THREAD_SCOPE_INVALID가 되어 계획의 RPC_PARAMS_FORBIDDEN과 다른 문제(당시67–74행), (F2) metadata threadId=[]/{}가 frozenset membership 전에 type 검사를 받지 않아 TypeError를 낼 수 있는 문제(당시182–183행)가 있었다. 이는 정적 경로 분석이며 실행 실패 기록은 아니다.

이어 원본과 실제 source의 diff를 확인하는 동안 source 및 source-ready가 03:30:14Z 갱신본으로 바뀌어, 추가 대기·polling 없이 해당 bytes/hash와 선언을 한 번 대조했다. 현재 위 `6f704f…` pin은 True의 len(tids)!=1 검사를 nonempty/UUID 검증보다 앞에 놓고, membership 전에 exact builtin str threadId를 확인한다. 두 finding은 모두 이 갱신본에서 해결됐다. 최초 pin에 PASS를 소급하지 않는다. reviewer가 제품을 수정한 것은 아니다. SOURCE_READY의 현재 pin과 실제 source hash가 일치하며, 보존 원본 hash도 원래 PR16 pin과 일치한다.

**read_only 범위:** 생성자는 exact bool만 받고 True의 빈/복수 고유 thread 집합을 import/connect 전에 PARAMS로 거부한다. 원래 endpoint·UUID 검증도 유지한다. True의 call 허용 method는 initialize/thread/read/thread/turns/list뿐이다. thread/read는 exact keys와 includeTurns is False, turns는 exact 5개 keys 및 cursor is None / builtin int limit1 / builtin str notLoaded / builtin str desc를 검사한다. thread는 단일 allowlist의 builtin str이어야 한다. 거부는 기존 call의 request ID 증가·송신보다 앞에서 발생한다. queue/items/resume/settings/account/config/new-thread 호출 경로는 새 모드에서 열리지 않는다.

**helper와 fail-close:** read_thread_metadata는 True에서 기존 call(thread/read, includeTurns=False)을1회 사용한다. thread dict와 요청한 thread.id의 일치를 확인한 후 기존 decoded result를 반환한다. initialize/notification/turns/페이지 후속/재접속은 자동 호출하지 않는다. wrong/missing/malformed thread는 RPC_THREAD_BINDING_MISMATCH이며 call의 ProbeError는 close 후 그대로 전달한다. 기타 Exception도 close 후 고정 RPC_METADATA_UNCONFIRMED로 바꿔 원문 오류 내용을 넣지 않는다. False 모드 helper도 METHOD 거부와 close를 수행한다. 기존 closed client는 재연결하지 않는다. 반환 원문은 비식별화 report가 아니며 새 저장/log/cache/ready 승격은 없다.

**기존 전송 로직 보존:** 보존 원본과 현재 source의 실제 diff는 생성자의 새 mode/단일 thread guard, 위 helper, _validate의 True 전용 분기 추가로 한정된다. METHODS 상수 및 False/default 기존 validator 경로는 유지한다. WebSocket handshake/frame/size·finite guard, JSONRPC request ID·completed ID·remote error 폐기, _send/call/notify_initialized, deadline 계산과 close 구현은 변경하지 않았다. 따라서 허용 metadata도 기존 실제 encode/send/decode/ID 대조 경로를 그대로 지난다. 원본 transport 반입 사실과 이번 변경 diff를 구분한다.

미해결 정적 finding은 없다. 후속 새4 검사/변이2 및 독립 실행근거 검토는 아직 필요하다. 그중 기존 차단 matrix 안에서 빈 집합 PARAMS/connect0, 비-str·hash 불가능 threadId의 PARAMS/send0을 확인하고 wrong-id method에서 helper mismatch/call 오류 후 실제 fake close를 단언해야 한다. 추가 method나 기존 PASS/suite 반복은 요구하지 않는다. 지정 두 mutant의 scope는 includeTurns/limit 단일 guard로 유지된다.

이번에는 source-ready·원본·선언 diff·실제 diff와 계획 계약을 정적으로 읽고 이 보고서만 작성했다. 제품/검사 편집·import/실행·추가 환경/socket/RPC/다른 agent 조회는 없다. CLOSED native 조사·scheduler PAUSE·원본11 및 과거 실행/최초 오류·운영/auth 보호를 유지한다. 기존 v3 현장 성공은 이 수정본의 native 인수가 아니며 수정본 native acceptance는 NOT_RUN이다.
