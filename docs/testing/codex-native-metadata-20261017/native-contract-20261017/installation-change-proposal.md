# 별도 검토 대상 설치·연결 변경안 (미실행)

현재 근거: service-observation.json의 service/timer inactive+disabled, 기존 PAUSE 존재. 기존 ExecStart는 /usr/bin/python3 -B /opt/codex-agent/nightly-v1/orchestrator.py run-once이며 workspace/automation_mvp 경로와 never/ephemeral 호출을 사용한다. console은 agent_project와 vms_project를 함께 열거한다. 이번 작업은 이 파일·unit·일정·PAUSE를 변경하지 않았다.

## 적용 순서와 담당

1. codex_shshin 소유자가 installation-comparison.md의 agent_project 전용 runtime-candidate 경로 또는 다른 전용 경로를 선택하고, 최종 검토된 commit·source manifest·expected UID를 승인한다. PR22 d59de21d4b43a71f3e518592c589b0761e668de5는 검토 소스 기준일 뿐 설치 승인값이 아니다. 후속 PR 변경이 있으면 최종 고정 commit/manifest를 사용한다. 현재 source mode0664와 marker 부재는 설치 적합 판정이 아니다.
2. 설치 담당자가 승인된 새 경로에만 고정 파일을 복사하고 marker/code_version, 일반파일·nlink1·UID1011·쓰기 권한을 검사한다. 기존 /opt나 automation_mvp 원본을 교체하거나 소유권을 변경하지 않는다. fixture loader의 7개 pin은 unattended/transport 전체를 검증하지 않으므로 전체 설치 manifest와 caller의 source pin 검사가 연결되기 전 native_ready로 올리지 않는다.
3. DB 소유자가 별도 승인된 전용 state에 필요한 새 DB와 스키마를 명시 생성한다. 기존 ledger/control DB 경로는 미제공이다. loader는 두 기존 DB를 검증할 뿐 init/migration하지 않는다. intent/binding/receipt DB 3개는 별도 API와 계약이므로 fixture DB 두 개로 대체하지 않는다. 기존 운영 DB를 이 명령에 사용하지 않는다.
4. controller 담당자가 소유자 승인 설정 및 사용자 지정 기존 thread 3개의 client identity/binding을 연결한다. native metadata의 agentRole/parentThreadId는 세 thread 모두 null이었다. 로컬 role 지정과 runtime 부여 역할을 혼동하지 않는다. metadata 응답에는 approvals_reviewer 증명이 없으므로 기존 실제 turn_context 근거를 별도로 유지한다.
5. queue/list 담당 유지관리자가 설치 버전 handler의 읽기 전용 부작용 계약을 소스 또는 공식 문서로 제공한다. generated schema만으로는 충분하지 않아 이번 queue/list 호출은 0회다. 추가 native 조사 권한은 이번 종료된 창에서 남겨 이월하지 않는다. queue/add·thread/resume·worker dispatch로 이를 시험하지 않는다.
6. 서비스 관리자에게 전달할 활성화안은 agent_project만 선택하고, sandbox/Auto-review/한도·중복 방지·checkpoint·독립 검토 게이트가 연결된 실제 launcher를 대상으로 해야 한다. 현재 그 launcher와 소유자 승인 config가 없어 새 ExecStart 명령은 확정할 수 없다. 기존 runner/console을 치환하거나 서비스를 enable/start하는 명령은 준비 완료로 제시하지 않는다.

## 검토 전 조회 명령 (이번 작업에서 재실행하지 않음)

```sh
systemctl show codex-nightly.service codex-nightly.timer -p Id -p ActiveState -p SubState -p UnitFileState -p MainPID -p User -p Group -p WorkingDirectory -p ExecStart -p ExecCondition -p ReadWritePaths -p ProtectSystem -p PrivateTmp -p NoNewPrivileges
test -f /etc/codex-agent/PAUSE
id -u codex_shshin
```

값을 출력하지 않는 PAUSE 존재 확인과 unit/account metadata 조회뿐이다. 서비스 관리자 후속 확인은 위 명령 각1회·합계10초 이내·재시도0이면 충분하며 token/auth/jobs/prompts/운영 DB 조회는 필요 없다. 추가 socket/RPC는 제안하지 않는다. 실제 설치·DB 생성·서비스 전환은 구체 파일 diff/고정 manifest/복귀안과 별도 승인이 준비된 뒤에만 수행한다.

현재 변경0이므로 복귀 명령도0이다. 후속 staging이 실패하면 새 전용 경로의 산출물만 근거를 보존하고 철회하며, 기존 unit/config/source·root/user PAUSE는 유지한다. service 전환과 scheduler 활성화는 본 Draft PR 범위에서 제외한다.
