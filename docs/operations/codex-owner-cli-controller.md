# 기존 소유 CLI와 controller 연결

PR24 `02724c5f7fbc0aadecebe2befdaf9f8990374869`의 intent 원장·파일 검증·lock·admission과 설치 패키지를 재사용한다. 운영 설치나 scheduler 활성화는 이 변경에 포함하지 않는다. 기존 PR24 준비본은 보존한다.

## 지원 계약과 미연결 경계

[공식 CLI queue](https://learn.chatgpt.com/docs/cli/reference#codex-queue)는 `--thread`, `--message`, `--remote`를 지원한다. 설치된 CLI의 help에서도 지원을 확인했다. JSON 접수 출력과 caller message-ID 옵션, queue ID→turn ID 대응은 확인하지 못했다. stdout·프로세스 종료 0을 native ACK로 해석하지 않는다.

[공식 turn 이력](https://learn.chatgpt.com/docs/app-server#list-thread-turns)은 resume 없이 조회할 수 있고 `itemsView=full`을 지원한다. cached 설치 버전0.162.1의 Turn/UserMessage/AgentMessage 스키마를 재사용한다. 이번 구현은 full turn page만 사용하며 별도 items RPC는 호출하지 않는다. `userMessage.clientId`는 owner가 실제 관측한 literal과만 대조하고 다른 종류의 ID와 동일하다고 가정하지 않는다.

자동 CLI→turn 매핑은 미연결이다. 지원 가능한 대안은 기존 소유 client에서 실제 turn과 user-message item을 한 번 확인하고 명시적 binding을 전달하는 방식이다. 로컬 intent의 `clientUserMessageId`는 LOCAL_ONLY claim 토큰으로만 사용한다. 이 토큰을 기존 native ACK/binding API의 실제 wire-ID로 넣지 않는다.

## 실제 코드 경로

`OwnerCLIController`는 caller가 소유한 기존 intent SQLite connection, private records 디렉터리, 기존 schedule policy와 명시 설정을 받는다. DB 생성·초기화·migration·close는 하지 않는다. 새 records 경로는 PR24 fixture state와 immutable 설치 tree 밖에 둔다.

1. `prepare`는 기존 reserve API로 intent bytes/hash를 저장하고 exact CLI argv·prompt를 private request에 보존한다. request SHA는 canonical UTF8 전체의 hash이며 자기 hash 필드는 없다.
2. `submit`은 기본 disabled다. 명시 설정·CLI entry pin·root/user/local PAUSE·시간·시작횟수·lock 검사를 통과하고, 이번 호출의 durable 신규 claim 확인이 모두 성공해야 CLI에 한 번 진입한다. CLI 대상은 기존 Unix endpoint를 `--remote`로 고정한다.
3. stdout bytes/hash와 프로세스 결과는 0600 private 파일로 보존한다. stderr는 hash/count만 보존한다. timeout·응답 유실·저장 불명확·이미 UNKNOWN인 intent는 자동 재제출하지 않는다. 원장 UNKNOWN은 완료 관측 뒤에도 재활성화하지 않는다.
4. `observe`는 owner binding이 없으면 RPC 0회로 UNBOUND를 반환한다. binding은 role/thread/turn/user-item/literal clientId/prompt hash/request hash/별도 CLI receipt hash를 포함한다. receipt 자체가 유실됐으면 None을 명시한다. 이미 확인한 terminal 결과는 보존하며 재조회로 대체하지 않는다.
5. 초기화된 기존 readonly owner client의 명시적 completion scope에서만 full turn history를 읽는다. 최대4페이지·페이지20개·20초·1MiB이며 재시도·resume·settings 변경·재연결은 없다. 불완전·모호한 이력과 inProgress/failed/interrupted/completed를 구분한다. completed에도 exact 사용자 입력과 final_answer가 필요하다.
6. `record_delivery`는 기존 `audit_bound_checks`로 tests/reviewer/policy의 head·binding·check snapshot을 대조하고 Draft PR의 repo/base/head/번호/open/Draft를 대조해 기록한다. 이 구조 비교는 입력 출처·실제 검사 실행·reviewer 독립성의 인증을 대신하지 않는다. native_ready·independent_review_verified·merge_authorized는 false다. 실제 검사 발행·PR 게시의 인증된 caller 연결은 후속 인수 대상이다.

## codex_shshin 계정의 후속 설치 절차 — 현재 미실행

이미 codex_shshin으로 로그인했으면 계정 전환 없이 진행한다. 다른 관리 계정에서는 다음과 같이 로그인 shell로 전환한다.

```sh
sudo -iu codex_shshin
```

전환한 shell에서 계정·UID·작업 경로를 확인한다. credential 파일이나 환경변수 값을 출력하지 않는다. HOME을 수동 대입하거나 root 계정으로 launcher를 실행하지 않는다.

```sh
set -eu
test "$(id -un)" = codex_shshin
test "$(id -u)" = 1011
cd /home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project
pwd
```

설치 담당자는 검토된 패키지의 `DELIVERY.json` code version·manifest·launcher hash·UID를 외부 기준으로 고정한 뒤 해당 `COMMANDS.md`의 hash 검증→stage→명시적 init-new→verify→status 순서를 따른다. 기존 state가 있으면 init-new를 호출하지 않고 기존 schema를 확인한다. 기존 DB의 자동 초기화나 migration은 없다. 복귀는 명령서의 no-overwrite rename으로 이번 candidate만 보존 퇴역시킨다.

PR24 기존 준비본의 명령서는 다음 위치에 보존돼 있다. 새 controller 소스를 PR24의 immutable bundle에 임의 복사하면 기존 manifest 검증이 거부하므로, 새 소스는 새 검토 commit의 manifest로만 준비한다.

```text
agent-execution-deliveries/build/20261017/package-82477695d924/COMMANDS.md
```

새 records의 미적용 후보는 다음과 같다. 기존 Git 제외 규칙 `**/var/`를 사용한다. 설치 승인 전에는 만들지 않는다. candidate의 fixture state와 구분된 경로이며 UID1011/0700, 파일0600을 요구한다.

```text
/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/owner-cli-state/var
```

기존 endpoint·role thread는 다음 관측/사용자 지정값을 재사용한다. 새 socket·thread·credential을 만들지 않는다.

```text
cli_path=/home/codex_shshin/.local/bin/codex
owner_endpoint=/home/codex_shshin/.codex/app-server-control/app-server-control.sock
test_agent=01a13fb0-1721-7af3-82e6-747eac4de411
reviewer=01a13fb0-1abe-7623-a15e-cf07071ce54a
submission_enabled=false
```

소유자가 초기화한 기존 client의 한 번 조회 형태는 `owner_client.read_turn_page(<위 역할 UUID>, None)`이다. controller가 새 client를 생성하지 않는다. 소유자는 조회된 정확한 turn_id/user-message item ID와 request/receipt hash를 확인해 binding을 전달한다. 이 후속 조치는 별도 승인된 native 조회가 필요하며 이번 작업의 추가 RPC 허용 횟수는 0이다. 확인이 불가능하면 UNKNOWN을 유지한다. 철회된 6-RPC 스크립트와 thread/resume은 사용하지 않는다.

실제 CLI 제출·native 완료·두 작업 연속 실행·운영 설치·서비스 전환·PAUSE 해제는 이번 로컬 검사와 구분해 NOT_RUN으로 남긴다.
