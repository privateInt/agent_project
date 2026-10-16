# U01-T1: 기존 Unix WebSocket 전송만 보정하는 검사 계획

ID: **U01-transport-v1**. 작성: 별도 test_agent. 독립 사전 검토 대기.
현재는 파일·로컬 dependency metadata/source만 읽었다. 새 연결/설정 변경/검사 실행은 하지 않았다.
기존 U01 전체 계획은 수정하지 않는다. 이번 보정은 scheduler/queue/무인 작업 활성화가 아니다.

## 고정 기준과 최초 실패

| 대상 | SHA-256 |
|---|---|
| `/tmp/agent-unattended-plan.md` | `57c43182b0ba07e1bb4ea0510e3b79b119fe6b761ea70cf58916c99cf8ef0d88` |
| 검토/최초 실행된 `/tmp/agent-unattended-runtime-policy.py` 및 보존 사본 | `48471f7dc2fc8c2b71ca25684857a9d77a76f87ec3b688dbe2f0e392fb430a98` |
| repo `var/unattended-20261015/transition-evidence/runtime-policy-first-run.json` | `da9e581f54dec7692ec65a4fe126b88dd12dab721ee193331c4adb277c98f7b0` |
| 재사용 `src/ops/tools/codex_readiness.py` | `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399` |

최초 host 실행1회는 `RPC_REQUEST_TIMEOUT`, settings_changed=[], threads={}이고 queue/
sandbox/global config/service 변경=false다. 스크립트는 initialize 반환 후에만 후속 RPC를
보내므로 이 기록·순서상 settings update에는 도달하지 않았다. 이것을 성공으로 바꾸지 않는다.
raw `codex app-server proxy`와 JSONL RpcClient가 실제 Unix WebSocket의 HTTP Upgrade/
메시지 framing과 맞지 않는다는 것은 coordinator의 공식 developer-commands1778 근거에
따른 **유력 원인**이며, timeout 기록만으로 입증된 wire trace라고 표현하지 않는다.

## 수정 허용 범위

- coordinator가 기존 스크립트의 전송 생성과 client 구현만 Unix WebSocket으로 바꾼다.
  기존 readiness 공용 JSONL client/main·이전 검사·global 설정은 수정하거나 실행하지 않는다.
- 설치된 `websocket-client 1.9.0`에 pre-opened AF_UNIX socket을 넘기는 경로만 사용한다:
  `WebSocket.connect('ws://localhost/', socket=sock, redirect_limit=0, suppress_origin=True)`.
  coordinator가 로컬 connect/_http 구현을 확인한 계약이며 이 작성자의 실제 연결 관찰은 아니다.
  `websockets 9.1`은 로컬 source에 Python 3.10에서 주의할 legacy loop
  인수가 있어 호환성을 가정하거나 설치/업그레이드/여러 fallback을 시도하지 않는다.
  library와 버전/API는 새 offline 검토·검사에 결합한다. 아직 실제 연결 성공 근거는 없다.
- endpoint는 coordinator가 **이미 확인·승인한 기존 Unix socket 하나**를 명시한다.
  socket 탐색·다른 UID 경로·TCP/remote WS·auth/token 파일·프록시 환경 fallback은 금지한다.
  `ws://localhost/` 같은 handshake URI는 이미 연결한 AF_UNIX FD의 HTTP Upgrade 용도만
  허용하며 localhost TCP 접속이나 DNS 탐색으로 전환하지 않는다.
- raw JSONL write/line split 대신 **WebSocket text message 하나당 JSON-RPC object 하나**를
  주고받는다. strict UTF-8/duplicate key/nonfinite 거부와 ID/type/result/error 검증을 유지한다.
  binary message는 고정 오류로 거부한다. library framing/fragment 처리와 JSON 파싱을 분리한다.
- 전체60초·개별 요청5초 **상한을 늘리지 않는다**. Unix connect/Upgrade/send/receive/close도
  남은 absolute monotonic deadline 안에서 제한한다. notifications마다 deadline을 재설정하지
  않는다. 무한 close/reconnect/자동 HTTP redirect·retry는 없다. 기존 메시지128KiB·누적512KiB·
  notification32 상한을 유지한다. frame 선언 길이와 fragmented message 누적 길이는 payload
  할당 전에 제한한다. 수신 후 string 길이 검사만으로 pre-allocation 제한을 증명하지 않는다.
  installed package source는 바꾸지 않고 transport 내부의 instance 경계에서 제한한다.
  client 소유 FD만 닫고 shared daemon/기존 thread/worker를 kill하지 않는다.
- 공개 오류는 고정 code만 남긴다. handshake headers·원격 error/body·token·raw exception은
  버린다. 새 thread/turn/goal 생성, queue·permission/profile/cwd/network/model/provider 변경,
  PAUSE 해제, global config/서비스/systemd 변경은 모두 없다.

## 그대로 유지할 RPC와 대상

대상 thread는 기존 세 개뿐이다.

```text
01a13f12-328d-7342-b285-a0868ac66447
01a13fb0-1721-7af3-82e6-747eac4de411
01a13fb0-1abe-7623-a15e-cf07071ce54a
```

request allowlist는 **initialize, configRequirements/read, account/rateLimits/read,
thread/resume, thread/settings/update**의 기존5개 그대로다. initialized는 initialize 후
notification1회만 보낸다. server→client request/approval은 수락하지 않고 중단한다.
세 baseline을 먼저 확인한 뒤, 필요할 때 각 thread에
`{threadId: 기존ID, approvalsReviewer: auto_review}` 두 필드만 update한다.
기존 cwd/on-request/workspaceWrite/networkfalse/roots 및 선택9필드 baseline/readback 검사를
변경하지 않는다. payload 추가·범위 확대·불명 입력 허용으로 transport 실패를 피하지 않는다.
managed requirements/quota와 비교 logic도 byte 보존한다. 이미 auto_review이면 update0이다.

## 새 offline 검사: 정확4개 method 후보

실제 Unix socket/daemon/모델을 쓰지 않고 fake WebSocket factory·fake clock·응답만 사용한다.
기존 전체 검사와 U01/L01 통과 target을 반복하지 않는다. 이 보정의 새 검사 최초1회만 수행한다.

| method | 입력·정확한 assertion |
|---|---|
| test_unix_ws_handshake_and_json_messages | 주입 factory에 승인 Unix path/소유 socket만 전달. JSON text 정상 응답·fragment가 합쳐진 message·notification을 파싱. raw JSONL proxy/subprocess/TCP/auth fallback0. 잘못된 UTF-8/JSON/duplicate/nonfinite/binary/shape/ID/중복ID/원격error/server request는 고정 오류와 후속 update0 |
| test_absolute_deadlines_and_bounds | connect/Upgrade/send/recv/close 지연·EOF·notification flood·oversize를 fake로 주입. request<=5/total<=60, deadline 연장0, notification<=32/message<=128KiB/누적<=512KiB. timeout이면 own FD 정리·reconnect/retry0, raw error 비노출 |
| test_exact_methods_threads_and_reviewer_only_payload | 정상 fake의 initialize→requirements→quota→세 baseline→필요한 update/readback 순서. initialized1회, baseline3개 이전 update0, update는 고정3ID·두 필드만·각최대1회. 다른method/ID/추가권한 필드는 wire송신0. 기존 선택9필드는 reviewer 외 그대로, 변경불명은 성공0 |
| test_uncertain_update_never_replayed | initialize 유실은 update0. 한 update send 후 ACK/마지막readback 유실이면 해당 pending ID·최초오류 보존, BLOCKED_OR_UNCERTAIN, 다음thread update0, 자동 재전송0. false/null quota는 작업 활성화로 승격하지 않음. queue/PAUSE/서비스/권한 변경0 |

transport 교체 전후 diff/AST source-segment를 대조해 threadIDs·method whitelist·모든 기존
비교/갱신 loop·write/checkpoint·sanitization의 business body 불변을 기록한다. 바뀌는 import/
client 생성 및 transport class만 허용한다. test/transport/script/plan/review SHA를 남긴다.
새 WebSocket 전송을 fake로 검사한 결과를 실제 연결 PASS로 표시하지 않는다.
새 전체 mutation이나 예전 suite 반복은 이 한정 변경에 추가하지 않는다.

## 승인 후 실제 host 실행: 최대1회

독립 reviewer가 정확한 script·새 transport/검사·이 계획 SHA 및 offline 결과를 승인한
경우에만 coordinator가 같은 cwd·같은3threads로 **한 번만** 실행한다. executable/argv/socket/
output을 구체화해 제한 host permission을 받고 전체60초/요청5초를 유지한다. 기존 sandbox
거부나 standalone 문제를 다시 probing하지 않는다. 시간 상한을 늘려 두 번째 시도하지 않는다.

실행 전 원래 script와 first-run JSON을 보존하고 현재 기록에 불명 pending이 없는지 확인한다.
새 결과는 기존 runtime-policy.json의 원문과 별도 WebSocket attempt 근거로 보관하며
first-run/첫 script/review를 덮어쓰지 않는다. 부분 update/응답 유실은 같은 pending ID로
남기고 이 계획에서 자동 복구·재update·롤백·추가 host 실행을 하지 않는다.

실제 통과선은 initialize ACK, requirements·sanitized quota 관찰, 정확3개의 before/readback,
필요한 reviewer-only update ACK, 비교9필드 보존, 그리고 root/user PAUSE·queue·services·
global config·sandbox/permission의 변경0이다. quota가 불명이어도 설정 관찰을 작업 실행
승인으로 해석하지 않는다. settings는 subsequent turns의 상태이며 실행 중 turn이 소급
변경됐다고 주장하지 않는다. 부분 결과는 BLOCKED/UNCERTAIN으로 보고한다.

이 호스트 확인이 통과해도 실제 queued 개발 작업2개는 **NOT_RUN**이다. root/operator
PAUSE·quota 불명·control RPC 불가 시 전체 무인 준비는 계속 미완료다. 새 권한·task·
계정·비용·PAUSE 변경은 이번 전송 보정에 포함하지 않는다.
