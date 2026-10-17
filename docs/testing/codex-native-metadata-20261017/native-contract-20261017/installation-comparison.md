# 설치 현장과 고정 검토 후보 대조

읽기 전용 확인 완료. 이 담당 RPC/재시도/검사/제품 import/DB 연결/설치 변경은 모두 0이다. 이전 D18 HELD 기록은 보존하고 이번 03:16:41–03:31:41Z의 제한된 재개와 구분한다. 조사 종료 후 추가 조회하지 않는다.

| 항목 | 현장 관찰 | 검토 기준·미확인 범위 |
|---|---|---|
| 서비스 | User/Group=codex_shshin, 계정 UID=1011; WorkingDirectory=/home/codex_shshin/workspace | 실행 중 PID/유효 UID·systemd 병합 상태는 조회하지 않음 |
| 상태 | orchestrator/flex/console 소스의 기본 state=/home/codex_shshin/automation_mvp | 해당 운영 디렉터리/DB/jobs/prompts를 열거나 탐색하지 않음 |
| 시작점 | ExecStart=nightly-v1/orchestrator.py run-once; 지정 dropin이 ExecCondition만 scheduler-v2/flex_gate.py로 교체 | 새 loader/owner adapter의 서비스 연결 근거 없음 |
| 기존 정책 | runner 소스는 --ephemeral, approval_policy="never", --sandbox workspace-write | 새 승인값/실제 owner 설정을 생성하거나 확인했다고 표시하지 않음 |
| 일정·PAUSE | schedule v2, 23:00–07:00 KST, max_starts=3; timer 5분마다/Persistent=false; /etc/codex-agent/PAUSE 존재·0bytes | PAUSE 삭제/일정 변경 없음. gate는 새 시작만 차단하며 프로세스 종료 보장이 아님 |
| 운영 소스 소유권 | 지정 /etc·/opt 파일은 이 관측에서 uid=65534, 초기 hash/uid/size와 전부 동일 | 계정 UID1011과 구별. mount/namespace 원인 추정·기존 파일 chown 없음; loader 설치 적합 판정 아님 |
| 콘솔 | PROJECTS에 agent_project와 vms_project; runner 참조=/home/codex_shshin/automation_mvp/orchestrator.py | 참조 대상 운영 파일 미열람. VMS·공용 콘솔 변경 금지, 새 경로는 agent_project만 |
| 새 SQLite config | 지정 schedule에는 installation_root/state_dir/ledger_db/control_db가 없음; 초기 관찰도 /etc 새 config 없음 | /etc 전체 탐색 없음. 새 installation root·DB의 실제 배치/스키마/UID는 미확인 |
| 역할 thread | 허용된 설정·unit·세 소스에 실제 역할 thread 매핑 없음 | 기존 owner thread 부재를 뜻하지 않음; owner metadata 조회는 총괄 영역이고 이 담당 RPC0 |
| PR22 | clean HEAD=d59de21d4b43a71f3e518592c589b0761e668de5 | 독립 final PASS 4ba79141…/146개 readback 기록 재사용; 검사·GitHub 재조회 없음. 설치/native 인수와 별개 |
| transport | 별도 bridge-development 파일 존재, SHA=7f70d9eb3402fddd97f2a8c0f448b2b3336db28512d0c8e004adc7dc76e4b415 | 별도 PR16 검토선; PR22 loader pin 목록에 포함되지 않음 |

현재 loader의 `_SOURCE_FILES`는 다음 **정확히 7개**다. hash는 PR22 개발 후보의 실제 bytes이며, /opt 설치본의 hash로 주장하지 않는다. 각 uid/mode/existence는 JSON에 별도 보존했다.

| 상대 파일 | 고정 후보 SHA256 |
|---|---|
| `src/__init__.py` | `a1f6eb99790b0c12a63bf5ae400c258fe034e6ad7876c4645c2dacdca7145f64` |
| `src/ops/__init__.py` | `39b0de92fc01d71e146b8ceb4719501ea7a985135dc9d8108c3daab9fe652284` |
| `src/ops/tools/github_check_audit.py` | `d2b8deb1b9a652d396c63f42d2c581af51c7c19aa9bfa7785f2e740580b3515e` |
| `src/ops/tools/github_fixture_collector.py` | `4a14a3f3412a44f4501266a4911e71a40b5acee5d50f07d27a4f7bf043e82ce0` |
| `src/ops/tools/github_fixture_ledger.py` | `5d1998196d63e647558f9c5dd93065b99896e4c6dc56f3d4ea91a75c997535b4` |
| `src/ops/tools/github_fixture_controller.py` | `671885e474d4eb2eafe4f786ea8f7075924bbd96250b77ff832561e1b943c5e7` |
| `src/ops/tools/github_fixture_config.py` | `63f5faf6a0bb828928d14bc97a173388ffaca8829f7bad4621a6981e8f180e88` |

loader marker는 `fixture-controller-installation.json`, exact `{schema:1, code_version}`이다. 개발 후보 root에도 marker가 없다. caller가 expected version·위 7개 pin·UID를 명시해야 하고 supplied expectation은 owner approval이 아니다. source는 exact UID/일반파일/nlink1/쓰기권한/namespace·module origin을 확인하며, 두 SQLite는 state 직속의 서로 다른 기존 0600 파일이어야 한다. loader는 init/migrate를 하지 않는다.

새 unattended 7개 부품(admission/intent/observer/quota/binding/receipt/owner ACK adapter) hash도 JSON에 보존했다. 이들은 현 loader 7개 pin에 포함되지 않는다. 현재 loader는 fixture 전용이며 실제 owner call/ACK/turn/native controller 연결을 완성하지 않는다. transport pin·client identity·실제 역할/승인값은 caller/owner 책임이다.

다음은 **미실행·미승인 후보 경로**다. owner approval/역할 thread 값은 null로 남겼다.

```text
config_file=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/runtime-candidate/pr22-d59de21d4b43/fixture-controller.json
installation_root=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/runtime-candidate/pr22-d59de21d4b43/installation
state_dir=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/runtime-candidate/pr22-d59de21d4b43/state
ledger_db=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/runtime-candidate/pr22-d59de21d4b43/state/fixture-ledger.sqlite
control_db=/home/codex_shshin/public-github-dev-pol48i5q/repositories/agent_project/var/unattended-20261015/runtime-candidate/pr22-d59de21d4b43/state/fixture-controller.sqlite
proposed_code_version=pr22-d59de21d4b43
candidate_expected_uid=1011 (계정 대조값일 뿐 실행 허가 아님)
```

1. 소유자 codex_shshin이 이 agent_project 개발 전용 목적지·버전·source manifest를 명시 승인한다. 설치 담당자는 고정 bytes/namespace와 marker를 새 root에 stage하고 UID/mode를 맞춘다. 기존 /opt·workspace·automation_mvp 원본은 교체하지 않는다.
2. DB 소유자는 별도 승인 뒤에만 새 전용 state/빈 ledger·controller 파일을 만들고 해당 기존 공개 API로 명시 초기화·schema/profile을 확인한다. 현재 DB 접근/init/migration은 실행하지 않았다. U01 intent/binding/receipt 3개 DB는 별도 계약이며 이 2개 config로 대체할 수 없다.
3. controller 구현 담당자는 unattended source/transport pin 및 caller-owned 연결 adapter를 별도로 연결해야 한다. owner는 실제 승인 설정·역할 thread/client identity 근거를 제공해야 한다. 어떤 값도 여기서 발명하지 않는다.
4. 서비스 관리자만 후속 독립 검토·명시 활성화 승인 후 별도 launcher/unit안을 적용할 수 있다. 현재 legacy ExecStart/console/VMS 경로를 새 adapter로 자동 치환하거나 timer를 시작하지 않는다.

복귀·PAUSE 조건: 현재 변경0이므로 복귀 조작도0이다. 후속 staging 전 현재 unit/dropin/schedule/source hash와 PAUSE를 보존하고 새 경로만 격리한다. 실패 시 새 staged 산출물만 근거 보존 후 철회하며 원래 경로·unit·config를 유지한다. 서비스 전환은 별도 복귀 검토가 필요하며 PAUSE/정지 상태에서만 진행한다. source/UID/marker/version/DB 계약/실제 owner 역할·승인 중 하나라도 없거나 불일치하면 차단 유지, 자동 초기화·재시도·dispatch·replay 금지다. /etc·legacy·root/user PAUSE를 해제하지 않는다.

RPC 총8 상한은 총괄이 누적 관리한다. 이 담당 RPC는 0이며 다른 담당의 사용량을 추정하거나 reset하지 않았다. 03:31:41Z 이후 본 재개 범위의 조사를 진행하지 않는다.
