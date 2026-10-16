# agent_project 전송 부품 최종 독립 검토

판정: **PASS — 고정 소스·검사·fixture와 범위별 fake 실행 근거에 한정**.
기존 정적 F1/F2는 해소됐다. 미해소 제품·검사 finding은 발견하지 못했다.
**최종 4-method 모듈 전체 실행은 NOT_RUN이다. live 실행을 승인하지 않는다.**
D18 보류를 유지하며 이 판정은 실제 Auto-review 적용·권한 인수·무인 개발 완료가 아니다.

## 정확한 검토 pin

개발 경로는 원 repo의 `var/unattended-20261015/bridge-development`다.
읽기 조회로 브랜치 `codex/agent-project-daemon-transport-20261016`와
HEAD/PR15 기준 `c862bfc6951491192a9fe98b535c9a8be921ee35`를 확인했다.
해당 작업 공간의 변경 목록은 전송 소스·검사·fixture 3개·요약 JSON·아래 문서 4개다.
admission 작업은 검토하거나 포함하지 않았다.

| 대상 — 개발 경로 기준 | SHA-256 |
|---|---|
| `src/ops/tools/codex_daemon_websocket.py` | `7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415` |
| `src/ops/tools/tests/test_codex_daemon_websocket.py` | `5085ce3d866a81afe7614cb6e4aeea785cf757da148395591dbc865ed0f3a9bf` |
| `src/ops/tools/tests/fixtures/codex_daemon_websocket/operation.py.txt` | `8a7308e1f3529f118065c7a256be3a3c82b970f9378d9ffdc67d3ef8bd7e647a` |
| 같은 fixture 디렉터리 `original-operation.py.txt` | `48471f7dc2fc8c2b71ca25684857a9d77a76f87ec3b688dbe2f0e392fb430a98` |
| 같은 fixture 디렉터리 `manifest.json` | `9f7a05ac526a0a6240a304df48db2b57e021742da78c1f508fb097f5e80db244` |
| `docs/testing/codex-daemon-transport-observed-20261016.json` — final review PENDING인 검토 시점 원문 | `f9d9ea1637bc170c792ab4a746049d086693b818640efa8a1575bd051c332acf` |
| 재사용 `src/ops/tools/codex_readiness.py` — 변경 없음 | `5b84534a6e0b3bb19fdd6a2bd6e3c38f23ea89edc205b461751b5c0e857ee399` |

fixture의 실제 경로는 `src/ops/tools/tests/fixtures/codex_daemon_websocket/`다.
`.py.txt`는 검사용 고정 원문이며 live 실행 허용 파일이 아니다.

| 검토 기준 | SHA-256 |
|---|---|
| `/tmp/agent-unattended-transport-plan.md` | `f6308eceb2ecfe48862fffb0bd4d592e88c820941251890957dbd4146bb08f6c` |
| `/tmp/agent-unattended-transport-plan-review.md` | `9dc87b9e674dbc2a86d1135e9814c3f67f351db888cd0fe9f9d6479552145b61` |
| `/tmp/agent-unattended-transport-static-review.md` — 최초 F1/F2 | `2e565a98a3e2062b2817e908860ff6d3507603323f41eb0fc7048aa0797b51fe` |

## 코드와 최초 finding의 해소

소스 235행과 최종 검사 838행 전체, 실제 fixture와 원문 diff를 읽었다.
AF_UNIX 소유 FD와 pre-opened socket, redirect 비활성, 명시 HTTP 101 확인,
로컬 5-method allowlist·thread/payload 제한은 유지된다. header 16KiB, frame/누적 fragment
128KiB를 payload 읽기 전에 검사하고 wire 512KiB·notification 32를 제한한다.
send/recv/connect는 남은 absolute deadline을 사용하며 notification으로 연장하지 않는다.
close는 소유 FD만 닫으며 graceful wait·shared daemon kill·reconnect·TCP fallback이 없다.
기존 decoder 경계와 그 fake 실행 근거를 재사용했으며 dependency를 재실행·갱신하지 않았다.

F1은 shared readiness parser 다음의 로컬 iterative finite 검사로 보정됐다.
dict/list 안의 exponent overflow도 RPC_NONFINITE_JSON으로 거부하며 공용 parser는 그대로다.
F2는 update ACK 때 settings_changed를 남기고, 같은 ID의 readback 검증이 완료된 뒤에만
pending_setting을 삭제한다. ACK 유실과 ACK 확인 후 readback 유실의 기록을 구분하고,
불명 상태에서는 다음 thread update·재전송·rollback으로 진행하지 않는다.

scope-decision의 좁은 보정과 실제 diff가 일치한다. 원문 업무 본문은 transport/import/별도
출력 경로와 검토한 F2 두 줄 외에 유지된다. 세 baseline 선행, 세 고정 ID, reviewer-only
두 필드 update, 선택 9필드 비교와 오류 기록을 약화하지 않았다.

## 실제 실행 근거와 재사용 범위

아래 경로는 원 repo의 `var/unattended-20261015/transition-evidence/` 기준이다.
각 result SHA를 현재 파일에서 계산해 요약 JSON과 대조했다. 실행 로그·marker·runner·
manifest·단계별 source/test diff와 audit를 읽었으며 reviewer가 검사를 실행한 것은 아니다.

| 단계 | 실제 관찰 결과·사용 범위 | result.json SHA-256 |
|---|---|---|
| `transport-run-1` | 최초 source18ee/test51f6/operationb5af의 4 methods PASS, FAIL/ERROR/SKIP 0. 설치 decoder와 fake socket, 메모리 업무 IO 근거다. 최종 pin 전체 실행으로 소급하지 않는다. | `8615b57a0b464f0bf2fefc508c0dbef8f2e194374b33c3afe446d761db5fa45d` |
| `transport-findings-before` | 새 F1/F2 helper만 FAIL2/ERROR0/SKIP0. F1은 inf 반환, F2는 ACK 목록을 남겼지만 pending=None인 의미 실패다. 최초 로그를 보존했다. | `f4ba1007c40a7f77ea6da702ad6c14f8a8512e603b7fb5080ab45195ab6c8c2c` |
| `transport-findings-after` | 동일 test helper bytes로 source7f70/operation8a73의 PASS2/ERROR0/SKIP0. executor는 **coordinator using test_agent two-target runner**이며 test_agent의 별도 재실행으로 표시하지 않는다. | `324cc4e272269c9cc6d3d5ba1c906db598c77b2a1795cc1602dc3f58c639f567` |
| `transport-test-reconciliation` | 새 strict business-diff audit helper만 PASS1/ERROR0/SKIP0. test_agent 실행. 4번째 method의 readback pending 기대값을 None에서 해당 ID로 강화했다. 기존 F1/F2 positive와 전체 4 methods를 반복하지 않았다. | `7e8c594d5428a469916d80909a243679065d3a5180f6b708cce38108b68f1ea3` |
| `transport-portability` | 최종 source7f70/test5085의 정상 업무·ACK 후 readback 유실 **두 helper만** 다른 이름의 fresh source tree에서 각각 PASS, ERROR/SKIP 0. test_agent 실행. | `a0199340d98f92520d0ae9801732ab46a9ad39f8af5080b568e3d5e5ebbe8aba` |

before/after test 전체 SHA는 동일한
`97d09ab155a8f01ca9944d34291876383d5b1684fa36614cb97d2ee6791a9bb7`이다.
reconciliation 후 test는 `0d4c2f025778b82a5778f9eb87220add9dcc083c04364c9550a148ce9adddb85`,
portability 후 test는 위 최종5085다. reviewer의 정적 AST/source-segment 대조에서도 F1/F2
helper는 before→after→reconciliation→final에서 동일했고, strict audit helper는
reconciliation→final에서 동일했다. 새 검사 실행 없이 이 결합을 확인했다.

run-1/before/reconciliation/portability의 해시 목록 12+14+13+22개 파일, 총61개는 모두
실제 bytes와 일치했다. after의 보존 입력 8개도 manifest의 SHA와 일치했다.
각 단계는 별도 결과·최초 오류를 보존하며 legacy/R03/R04/R15·옛 4 methods를 반복하지 않는다.

## 최종 네 method의 정합성과 portability

최종 unittest method는 계획의 네 개 그대로다. F1은 첫 method의 하위 helper,
strict diff audit/portable success는 세 번째, F2는 네 번째 method에 연결돼 있다.
세 번째 method의 원문 비교는 원래48471을 그대로 고정한 뒤 **검토한 F2 두 줄만** 명시적으로
치환하고 나머지 업무 bytes 및 transport 변경을 정규화한 AST를 비교한다. 단순한 비교 제거가
아니다. 네 번째 method의 기존 readback 행도 같은 pending ID를 요구하므로 옛 기대값과
새 helper가 충돌하지 않는다.

WORK/fixture 위치는 현재 test 파일에서 구하고 raw fixture SHA는 실행 전에 고정한다.
새 adapter는 단 하나의 top-level REPO 대입 한 줄만 메모리에서 치환하며 다른 bytes·AST를
대조한 뒤 guarded exec한다. 원본 업무 알고리즘을 별도 복제한 성공으로 대체하지 않았다.
동작 전에 fake client와 write/chmod/replace 및 socket/process 거부를 설치한다.

portability 실행은 격리 Python과 새 이름의 복제 트리에서 test/product/readiness를 import했다.
접근 guard는 해당 복제 트리와 설치 라이브러리 읽기만 허용했고, 사적 checkout/operator 경로
의존·실제 쓰기/socket/process 시도는 없었다. raw fixture는8a73 그대로, 적응 후 SHA는
`18c92160d7e8c17eaade7c09d4c2e941c7c1070c72fb7825de8b325e8630b8ba`이며 REPO 한 줄만 다르다.
정상은 세 reviewer 변경과 readback·pending 해제, 유실은 첫 ID의 ACK와 pending·최초 오류 및
후속 update 중단을 관찰했다. 실제 runtime 출력 파일도 생성하지 않았다.

따라서 변경되지 않은 framing/bounds·업무 guard의 최초 근거, 수정 두 지점의 동일 반례,
정합성 audit 및 fixture 공통 실행 경로가 바뀐 두 사례의 portability 근거를 범위별로 재사용한다.
최종 네 method의 구조·기대값은 일관된다. 다른 모든 parameterized case가 최종 pin에서 다시
실행됐다는 주장은 하지 않는다. 최종 whole-four-module 및 legacy suite는 **NOT_RUN**이며
이 판정에 불필요한 전체 재실행을 요구하지 않는다.

## 문서·요약과 기록 해석

네 문서의 실제 diff와 전송 관련 본문을 읽었다. 첫4 PASS·새 반례 FAIL→PASS·정합성·이식성
근거를 구분하고 실제 Auto-review/controller/두 작업을 미완료로 두는 서술이 요약과 일치한다.

| 문서 | SHA-256 |
|---|---|
| `docs/HANDOFF_NEXT_SESSION.md` | `8280648297c2855ca73dadcae8bb0b7854123ed9f5516ad47e55bc9f2ae97794` |
| `docs/development.md` | `8b09e5a8fbfd7e24e5196c13f7c08d2cf7d28c5ffa7fcff811f8684936c220f7` |
| `docs/roadmap.md` | `9d43c5c4b96b84cddff6fb80539aae9c5b95dea00a93ad92871fe30fc8ccaf2e` |
| `docs/status.md` | `71ceaf3976c7cfa85c2c6b41ab0542921e0c8fded0962ee4dde1b4b4d2bfd77a` |

비차단 기록 주석: `transport-findings-after/manifest-before.json`의 transport.py `bytes`는
이전9132가 남아 있지만 실제 보존 bytes는9698이며 SHA는 정확한7f70이다. 해당 runner도
SHA로 결합한다. 이 과거 size 값은 현재 파일 크기 근거로 쓰지 않는다. 최초 원문을 수정할
필요 없이 이 주석과 함께 보존한다. 코드·반례·결과 pin이나 PASS 판정에는 영향을 주지 않는다.

검토 시점 요약의 `final_independent_review.status=PENDING`은 자기 검토를 앞서 PASS로
쓰지 않은 메타데이터다. 판정 후 그 review 정보만 갱신·복사할 수 있고 새 요약 SHA는 다시
기록해야 한다. 위 f9d9 pin은 검토 시점 원문으로 유지하며 실행 횟수·source/test/fixture pin·
NOT_RUN/live 제한을 바꾼 요약에는 이 PASS를 소급하지 않는다.

## fake와 live의 한계·다음 단계

이 PASS는 fixture 전용 전송 부품의 코드와 제한된 실행 근거에 대한 독립 판정이다.
실제 WebSocket Upgrade/daemon 지원/설정 변경/Auto-review 적용/managed native 권한/
ordinaryUsageAllowed의 live 값/controller queue·내구성/무인 두 작업은 미인수다.
최초 raw proxy initialize timeout을 성공으로 바꾸지 않는다. 요약의 과거 runtime 관찰은
이번 reviewer가 현재 서버에서 새로 확인한 사실이 아니다.

**live 시도는 승인하지 않았고 수행하지 않았다.** 초기 계획의 미래 host 실행 절차보다
최신 사용자 지시와 D18 held가 우선한다. root/user PAUSE·worker/scheduler 보류,
vms 제외, 운영 원본·DB·Docker·credential 보호와 원본11개 변경을 유지한다.

다음은 coordinator의 게시용 근거·이 검토 복사, transport 전용 commit/Draft PR 및 게시 bytes
readback 단계다. main 병합·서비스/권한 설정 변경·native 조사나 live 실행은 포함하지 않는다.
이번 reviewer의 쓰기는 `/tmp/agent-unattended-transport-final-review.md` 하나뿐이다.
제품·검사·문서·계획 수정, 검사 실행, socket/daemon/credential/permission 조사,
다른 agent polling/wait·위임·통지는 수행하지 않았다.
