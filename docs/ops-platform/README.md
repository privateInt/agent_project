# Ops Platform 현재 범위

Ops는 Model·Data·Knowledge·Agent의 공식 API와 관측 백엔드를 조회한다. 업무 상태와 Tool/Agent 자산을 대신 소유하지 않는다. 관측 저장소·수집기·n8n은 infra, 조회 API/화면은 Ops, host 운영 제어는 별도 도구에 둔다.

## 단계별 상태

| 영역 | 코드와 확인 범위 | 미완료/보류 |
|---|---|---|
| O01 관측 기반 | Prometheus·Node Exporter·cAdvisor·GPU collector, Langfuse 기동과 보존 검사 | 지원 환경 확대·장기 용량/보관 인수 |
| O02 API와 추적 | 4개 플랫폼 조회, Agent metadata span·ToolCall/단계 연결 | 전체 실행/성공 답변을 모두 확인한 것은 아님 |
| O03 통합 화면 | 상태·자원·Agent 상세/추적·오류 분리·LAN 접속·재시작 보존 | 다중 사용자 권한·일반화된 성능/부하 |
| O04 경보 | 규칙 평가·Alertmanager 수신·Ops 표시·파일시스템 감시 | 외부 채널 발송·nonempty 상태 의미 보존의 전체 인수 |
| 발견/자동갱신 | Compose discovery·systemd/boot 제어, 10월 2일 명시 복구 후 새 정상 평가 1회 | 실제 재부팅·장기 운영 인수 미확인, Kubernetes 실제 배포 인수 미완료 |
| 백업/복원 | 운영 plan PASS, 실행기 구현·오프라인 검사, 기존 격리 파일 복원 | 운영 측정/수집/복원은 보류. public `measure` 차단, `backup_ready=false` |

이번 운영 범위는 worker 복구 확인과 문서 정리로 마무리한다. 다음은 작업 경과를 바탕으로 확장 계획과 품질 개선을 논의한다. 전체 현재 상태는 [상태표](../status.md), 후속 논의는 [로드맵](../roadmap.md), 백업 경계는 [백업 안내](../operations/backup-restore.md)에서 관리한다. 최신 [worker 복구 결과](../testing/ops-worker-recovery-closure-observed-20261002.json)는 짧은 관찰과 새 평가 1회의 근거다.

## 코드 진입점

| 책임 | 코드 |
|---|---|
| 조회 API·세션·웹 UI | [server](../../ops_platform/server.py), [web](../../ops_platform/web/) |
| 플랫폼 조회·자원·관측·경보 | [client](../../ops_platform/client.py), [service](../../ops_platform/service.py), [resource profiles](../../ops_platform/resource_profiles.py), [observations](../../ops_platform/observations.py), [alerts](../../ops_platform/alerts.py) |
| 발견과 설정 반영 | [Compose](../../ops_platform/ops/discovery_compose.py), [Kubernetes](../../ops_platform/ops/discovery_kubernetes.py), [refresh](../../ops_platform/ops/refresh_discovery.py) |
| worker/서비스 수명주기 | [worker](../../ops_platform/ops/refresh_worker.py), [boot](../../ops_platform/ops/boot_lifecycle.py), [systemd](../../ops_platform/ops/service_systemd.py) |
| host 백업 도구 | [ops_tools 안내](../../ops_tools/README.md) |

Ops web은 Docker socket을 마운트하지 않는다. 환경 발견/유지보수 도구의 host 권한을 조회 web의 권한과 혼동하지 않는다. 관측 장애가 원래 Agent 실패를 성공으로 바꾸거나 업무 실행을 재호출해서는 안 된다.

## 운영과 근거

- 현재 계약: [관측/발견](observability-discovery.md), [worker 수명주기](worker-lifecycle.md), [백업 범위](../operations/backup-restore.md), [실행기](../operations/ops-backup-execution.md).
- 실행/설치 참조: [Ops 앱](../../ops_platform/README.md), [관측 infra](../../infra/observability/README.md), [Langfuse](langfuse.md), [host 도구](../../ops_tools/README.md).
- 과거 결과: [초기 통합 이력](../history/ops-platform-202609-20261001.md), [후속 날짜별 원문 목차](../history/ops-evidence-catalog.md), [마지막 worker 복구](../testing/ops-worker-recovery-closure-observed-20261002.json).

오래된 실행 명령·그때의 pending/다음 단계는 현재 실행 지시가 아니다. 압축 이력에서 원문을 조회하며 소스/fixtures/candidate/source pins는 유지한다.
