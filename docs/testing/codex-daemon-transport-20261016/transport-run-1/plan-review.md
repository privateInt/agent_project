# U01 transport 보정 계획 독립 사전 검토

판정: **PASS — U01-transport-v1의 전송 구현과 새 fake-only4 methods 계획 승인**.
이 판정으로 host 재실행·실제 연결 성공·설정 적용·무인 작업 시작을 승인하거나 보고하지
않는다. 구현/검사 후 exact script·dependency·endpoint·결과의 독립 최종 검토가 남는다.

| 대상 | 고정 값 |
|---|---|
| 계획 | `/tmp/agent-unattended-transport-plan.md`, U01-transport-v1 |
| 계획 SHA-256 | `f6308eceb2ecfe48862fffb0bd4d592e88c820941251890957dbd4146bb08f6c` |
| 보존 business script | `48471f7dc2fc8c2b71ca25684857a9d77a76f87ec3b688dbe2f0e392fb430a98` |
| 보존 첫 runtime 결과 | `da9e581f54dec7692ec65a4fe126b88dd12dab721ee193331c4adb277c98f7b0` |
| 기존 공용 readiness | `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399` |

계획 원문, 보존 script와 RpcClient, 로컬 websocket-client의 실제 connect/http/handshake/
frame 경계 소스 위치와 공식 protocol 문서를 읽었다. dependency를 import·실행하거나
패키지 설치/업그레이드하지 않았다. 로컬 purelib는 `/usr/local/lib/python3.10/dist-packages`이며
그 아래 websocket 소스를 읽었다. 처음 예상한 다른 package 경로들의 파일 부재는 daemon/
제품 probe 결과가 아니다.

## 승인하는 구현 경계

- 기존에 지정한 Unix endpoint 하나에 연결한 **소유 AF_UNIX FD**만 사용한다. 그 FD를
  WebSocket.connect의 socket 인수로 넘기고 redirect_limit=0, suppress_origin=True를 쓴다.
  새 TCP/DNS·환경 proxy·다른 endpoint·standalone/daemon start·credential 파일 경로를
  fallback으로 추가하지 않는다. 로컬 `_http.connect`의 pre-opened socket 분기가 반환하는
  경로와 redirect 비활성 조건을 실제 구현 diff에 연결한다.
- 기존 script의5 method allowlist, 세 thread IDs, initialize/initialized와 모든 baseline/
  update/readback loop, pending/checkpoint/sanitization은 byte/AST 의미를 보존한다.
  허용 변화는 transport/import/client 생성과 **새 별도 attempt 출력**뿐이다. 공용 JSONL
  readiness client와 그 기존 검사·전역 설정을 수정하거나 반복하지 않는다.
- frame/message의128KiB, 누적512KiB, notifications32, 요청5초·총60초를 유지한다. connect,
  Upgrade, send, receive, close 모두 같은 absolute monotonic deadline의 남은 시간 안에
  둔다. notification/부분 수신마다 deadline을 갱신하지 않는다. payload 할당 전에 frame
  선언 길이와 fragmented 누적 message 길이를 거부한다. package 자체는 고치지 않는다.
- WebSocket text message 한 개가 strict JSON object 한 개다. binary/잘못된 UTF-8/duplicate/
  nonfinite/잘못된 ID·shape·result/error/server request는 고정 공개 오류와 중단이다.
  raw handshake·remote error·exception/credential 내용을 결과에 기록하지 않는다.
  shared daemon/process를 kill하지 않고 client가 소유한 FD만 정리한다.

## 네 method의 필수 검토 해석

계획의 네 method를 추가/삭제 없이 승인한다. 아래는 계획이 요구한 올바른 Upgrade·preallocation·
수명·비노출을 판정할 구체 조건이며 새 다섯 번째 테스트나 다른 제품 기준을 추가한 것이 아니다.

1. **handshake/json:** 로컬 library는 redirect_limit=0이어도 redirect 응답 객체를 남길 수
   있으므로 **성공 HTTP status101을 명시적으로 확인한 뒤에만 JSON-RPC를 전송**해야 한다.
   3xx/비101에서 TCP 재접속·redirect·업무 RPC 송신은0이어야 한다. 잘못된 데이터와 binary/
   request/ID/error의 각 음성 결과를 fixed code로 확인하고 headers/body를 드러내지 않는다.
2. **deadline/bounds:** 이미 만들어진 oversized string을 돌려주는 fake WebSocket만으로는
   pre-allocation 제한을 입증할 수 없다. fake pre-opened socket/stream의 frame header를
   **실제 transport decoder/installed frame-buffer 경계**에 공급하고, 큰 선언 길이 및
   fragment 누적 초과에서 payload read/할당이 시작되지 않았음을 call count/요청 길이로
   확인한다. HTTP Upgrade/헤더 수신도 wire·deadline 제한 밖으로 빠지면 안 된다. 마감 뒤
   기본 graceful-close wait를 새로 시작하지 않고 소유 socket을 bounded하게 닫는다.
3. **methods/threads/payload:** 실제 보정된 business 경로를 fake factory/IO로 격리하여
   baseline3개 전 update0, initialized1회, 승인3ID·두 update 필드·각 최대1회, 선택9필드
   보존을 확인한다. fake 테스트가 top-level script import로 실제 socket/기존 결과 경로를
   건드리지 않도록 먼저 격리하고, 이를 별도 복제한 업무 알고리즘의 성공으로 대체하지 않는다.
4. **uncertainty:** initialize 유실 update0, update send 뒤 ACK/마지막 readback 유실의
   pending·최초 오류·다음 thread update0과 자동 재전송0을 확인한다. partial/unknown을
   성공·미실행·추가 실행 권한으로 해석하지 않는다. 같은 기존 설정 intent를 유지한다.

처음 fake4개만 실행하고 실패하면 최초 source/test/plan/review·로그를 남겨 해당 실패만
고친다. D17/D18 상한과 이전 native 소진 보류를 유지한다. 이번에는 새로운 business guard
변이를 선정하지 않는다. 네 직접 malformed input/경계 검사가 이 framing 구현의 거부 지점을
실제 읽기/송신 counter로 확인하고 업무 판정은 불변이므로 별도 mutation 반복을 요구하지
않는다. 이를 기존 L01 변이가 WebSocket 구현을 검증했다는 뜻으로 사용하지 않는다.

## 다음 host 단계는 별도

공식 문서는 stdio=JSONL, Unix endpoint=WebSocket Upgrade로 구분한다. JSONL over raw proxy
불일치는 현재 timeout의 유력 설명이지만 실제 wire 원인 확정은 아니다.
[OpenAI 공식 developer commands](https://learn.chatgpt.com/docs/developer-commands)

offline4개와 전후 diff·scope·모든 pin을 받은 뒤 정확한 script/argv/socket/출력에 대해
별도 독립 검토한다. 그 뒤에만 이미 사용자 범위에 있는 **추가 host 실행 최대1회/총60초**를
구체화할 수 있다. 현재 이 reviewer의 신규 host 실행은0이고 이 문서 자체는 실행 승인이 아니다.
첫 script/timeout JSON을 보존하고 새 attempt 파일에 원래 hash·같은 intent를 연결한다.
기존 결과를 덮어쓰거나 자동 reconnect/retry/rollback을 넣지 않는다.

root/user PAUSE, scheduler/queue, sandbox/permission/cwd/model/provider/global 설정과 VMS는
그대로다. quota 불명은 모델 작업 허용이 아니며 실제 queued 개발 작업2개는 NOT_RUN이다.
별도 U01a admission 후보의 계획/구현도 이 transport 판정과 분리한다.

reviewer는 본 사전 검토 문서만 작성했다. 제품·검사·계획을 바꾸거나 실행하지 않았고
socket/RPC/daemon/probe/credential/권한/서비스 작업·추가 위임·MCP 통지를 수행하지 않았다.
