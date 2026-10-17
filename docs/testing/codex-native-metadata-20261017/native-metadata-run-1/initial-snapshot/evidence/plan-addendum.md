# D29 계획 보정 — 실행 계약 재사용과 최소 caller 연결

기존 계획을 보존하고 다음 차이만 검토한다. 새 metadata caller는 기존 UnixWebSocketRpc 객체를 직접 사용한다.

- turns/list exact params는 실제 v3 성공과 같은 threadId/cursor=null/limit=1/itemsView=notLoaded/sortDirection=desc이다. 다른 cursor·sort/type·추가 key는 송신 전에 거부한다.
- 같은 기존 client에 read_thread_metadata(thread_id)를 추가한다. 초기화는 caller 책임이며 자동 실행하지 않는다. 명시 read_only=True인 객체에서 thread/read(includeTurns=false) 1회만 호출하고, decoded result.thread.id를 요청 ID와 비교한 후 기존 decoded dict를 반환한다. 불일치/호출 오류는 close 후 재전송 없이 예외를 전달한다. 반환 원문을 로그/DB에 저장하지 않고 사용 가능·권한·native_ready로 올리지 않는다.
- helper가 turns/list를 자동 호출하지 않는다. wire 검사에서 helper 1회와 별도 turns call 1회를 실제 기존 encode/decode 경로로 확인한다. wrong-id 검사에는 thread.id 불일치와 helper 실패 후 close도 포함한다. 기존 네 methods와 두 지정 semantic mutants의 수·정상 PASS 반복 금지는 유지한다.
- 현장 v3의 7RPC 성공은 수정한 제품 소스의 native 인수가 아니다. 새 제품 검사는 실제 파일·전송 경로를 fake socket으로 실행하고, 배포본 native acceptance는 NOT_RUN으로 남긴다.
- 조사 창은 이미 03:25:25Z에 닫았으며 남은 RPC 예산은 이월하지 않는다. 로컬 구현 단위는 04:00:00Z까지 제한하되 D17 같은 실패 수정3회·기존 제한을 완화하지 않는다.
