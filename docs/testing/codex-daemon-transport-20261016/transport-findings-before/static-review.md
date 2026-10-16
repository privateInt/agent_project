# U01 WebSocket 전송·operation 독립 정적 검토

판정: **REQUEST_CHANGES — 구체적 정적 finding 2건**.
실행 PASS 또는 host 실행 승인이 아니다. 새 검사 파일·실행 결과는 검토하지 않았으며
test_agent를 polling하거나 기다리지 않았다.

| 실제 읽은 대상 | SHA-256 |
|---|---|
| `var/unattended-20261015/bridge-development/src/ops/tools/codex_daemon_websocket.py` | `18ee8259ab9f075dea32147351eabd7562c9f0fad67bc07ec3e720c9af938c70` |
| `/tmp/agent-unattended-runtime-websocket.py` | `b5af1109e0febdbc88dca395afbe598dfd62121723d44ed6a7c2c4a3cb58fa35` |
| 기준 계획 `/tmp/agent-unattended-transport-plan.md` | `f6308eceb2ecfe48862fffb0bd4d592e88c820941251890957dbd4146bb08f6c` |
| 기준 계획 PASS `/tmp/agent-unattended-transport-plan-review.md` | `9dc87b9e674dbc2a86d1135e9814c3f67f351db888cd0fe9f9d6479552145b61` |
| 보존 업무 원문 `/tmp/agent-unattended-runtime-policy.py` | `48471f7dc2fc8c2b71ca25684857a9d77a76f87ec3b688dbe2f0e392fb430a98` |
| 원 repo 및 개발 repo의 `src/ops/tools/codex_readiness.py` — 동일 | `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399` |

아래 line은 이 표의 고정 소스 기준이다. 실제 Python/module/library를 import하거나 실행하지
않고 텍스트·diff·SHA와 설치된 websocket/json decoder 소스만 읽어 판정했다.

## F1 · P2 · JSON 지수 overflow로 생기는 nonfinite 값은 거부되지 않음

위치: transport **203행**의 `rpc._parse(raw)`와 재사용 readiness **43–48행**.

수신 파서는 `parse_constant`로 NaN/Infinity 토큰을 막지만 `parse_float`는 지정하지 않는다.
예를 들어 현재 request ID에 맞는 text response
`{"id":1,"result":{"n":1e400}}`의 지수 숫자는 기본 `float` 경로에서 infinity가 된다.
이는 `parse_constant` 경로가 아니다. 결과 dict의 float를 검사하는 단계도 없으므로
transport의 result 반환까지 도달할 수 있다. notification 안의 같은 숫자도 거부되지 않는다.
계획의 nonfinite 거부 계약을 충족하지 않는다.

정적 근거: 로컬 Python 3.10 `json/decoder.py` 299–310·320행은 지수 숫자에 기본 float,
NaN/Infinity 세 토큰에만 parse_constant를 사용한다고 명시한다. `json/scanner.py`
49–61행도 두 분기를 나눈다. 이 입력을 실제 실행한 결과라고 보고하지 않는다.

이 결함은 기존 파서에서 상속한 것으로, 기존 readiness 검사 PASS를 취소하거나 전체 검사를
다시 실행할 이유는 아니다. 공용 readiness를 바꾸지 않는 transport 내부의 finite 검증 또는
엄격한 float parsing으로 보정할 수 있다. 계획의 기존 첫 번째 fake-only method의 nonfinite
하위 사례에서 overflow 숫자도 고정 공개 오류로 차단되는지 확인하면 된다. 다섯 번째 method나
새 mutation은 요구하지 않는다.

## F2 · P2 · update ACK 후 readback 유실에는 미확인 대상의 pending ID가 남지 않음

위치: operation **40–46행**, 특히 **42–43행**, 오류 기록 **49–51행**.

baseline은 정상이고 해당 thread의 reviewer를 바꿔야 하는 경우를 보면, 40행에서 pending ID를
기록하고 41행에서 update한다. ACK 직후 42행은 `settings_changed`에 ID를 추가하면서
`pending_setting`을 삭제·저장한다. 이어지는 43행의 resume/readback에서 응답이 유실되면
오류 경로는 BLOCKED_OR_UNCERTAIN만 기록하며 pending ID나 별도 readback 미확인 ID를 복원하지
않는다. 따라서 계획의 네 번째 method와 기존 PASS 해석에서 요구한 "마지막 readback 유실의
pending ID 보존"은 현재 코드로 충족되지 않는다.

`settings_changed`와 해당 thread의 before-only 기록은 남으며, 다음 thread update와 자동
재전송도 수행하지 않는다. 이를 update 미실행이나 전체 성공으로 오인하는 분기는 확인하지
않았다. finding은 **ACK 확인과 readback 미확인의 단계별 대상 기록 부족**이다.
update ACK 자체가 유실되면 40행의 pending ID가 남는 경로와 구분한다.

이 또한 보존 업무 원문에서 상속된 동작이다. 전송 교체의 회귀라고 보고하지 않는다.
업무 본문 보존 조건과 새 계획의 pending 요구를 일치시키는 최소 보정이 필요하다. ACK 근거는
그대로 두고 해당 ID의 readback 미확인 상태를 검증 완료까지 별도로 남기는 방식 등이 가능하다.
원문·첫 오류는 보존하고 자동 재update/rollback은 추가하지 않아야 한다. 검증은 계획의 기존
네 번째 fake-only method 범위다. reviewer는 그 보정을 직접 수행하지 않았다.

## 정적으로 확인한 구현 부분

- 실제 허용 diff는 import/path 추가, client factory 교체, 별도 attempt 출력 파일명뿐이다.
  세 thread IDs·baseline 3개 선행·reviewer-only 두 필드·9필드 readback·sanitization·오류 loop는
  보존 원문과 동일하다. diff의 exit 1은 차이가 존재한다는 정상 의미이며 검사 실패가 아니다.
- transport는 독립 METHODS와 `_validate`를 사용한다. 공용 RpcClient.call의 전역 allowlist를
  상속하는 문제는 없다. operation의 기존 `rpc.ALLOWED_METHODS` 대입은 별도 import한 모듈에만
  남아 있고 이 transport의 method 판정에는 사용되지 않는다. 공용 파일 변경은 없었다.
- AF_UNIX 소유 FD를 pre-opened socket으로 전달하고 redirect_limit=0·suppress_origin을 쓴다.
  설치 `_http.connect`의 pre-opened 분기는 TCP/DNS 경로 전에 반환한다. 명시 status 101 확인은
  업무 JSON-RPC 전에 있다. 별도 fallback/reconnect/daemon 제어 경로는 보이지 않는다.
- socket recv의 header 16KiB·전체 wire 512KiB 제한과 frame-buffer의 선언 길이·fragment 누적
  128KiB 제한이 있다. 설치 decoder에서는 이 recv_length hook이 payload recv_strict보다 먼저
  호출된다. payload 할당 전 제한을 의도한 위치에 연결한 것은 확인했지만 실행 증명은 아니다.
- 소유 socket의 각 send/recv와 connect 전에는 absolute deadline의 남은 시간을 적용하며
  notification은 request deadline을 갱신하지 않는다. close는 graceful wait나 shared process
  kill 없이 shutdown/소유 FD close만 한다. library 예외·remote error는 고정 code로 바꾼다.

## 남은 판정 경계

두 finding의 최소 보정·해당 새 fake-only 검사 근거와 exact pin에 대한 최종 검토가 남는다.
현재 테스트 4개 실행 결과, 실제 Upgrade/연결·설정 적용·native 권한 격리·무인 두 작업은
이 리뷰의 PASS 대상이 아니다. host 재실행 명령을 실행하거나 승인하지 않았다.

새 operation의 출력명은 첫 `runtime-policy.json`과 다르다. 미래 실제 실행 단계에서는
원래 script/첫 결과 hash·동일 intent와 새 attempt 근거의 결합도 보존해야 한다. 이 리뷰에서는
기존 결과를 재조회하거나 덮어쓰지 않았다.

reviewer는 요청된 검토 파일 외 소스·검사·계획을 수정하지 않았다. 검사 재실행·socket/RPC/
daemon/runtime/credential/운영 접근·추가 위임·통지는 모두 0이다. root/user PAUSE, vms 제외,
기존 로컬 변경·기존 결과·최초 오류 이력과 기존 sandbox 조사 상한/소진 보류를 유지했다.
