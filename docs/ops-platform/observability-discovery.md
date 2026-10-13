# Ops 관측·조회·환경 발견 계약

갱신: 2026-10-07 코드 기준. 과거 구현/수정/검사 명령은 [통합 이력](../history/ops-evidence-catalog.md)에 보존한다. 현재 완료 범위는 [상태표](../status.md)이며 문서 정리 때문에 이미 완료한 기동·인수·추론을 반복하지 않는다.

## 조회와 관측

Ops는 네 플랫폼 공식 API, Prometheus 자원/경보, Langfuse 메타데이터를 읽는다. 원래 Agent 실패·업무 상태를 수정하거나 관측을 얻기 위해 업무를 재호출하지 않는다. Ops web에는 Docker socket을 마운트하지 않는다. infra는 수집기/n8n/관측 저장소, Ops는 조회 API/화면, host 도구는 환경 발견/운영 제어를 소유한다.

| 책임/소유 코드 | 현재 계약 |
|---|---|
| [client](../../src/ops/platform/client.py), [transport](../../src/ops/platform/transport.py) | 공식 목록/상세·연관 Operation/단계/ToolCall GET-only. 조회 성공은 업무 성공 아님; 표본·페이지 예산·부분 결과 표시 |
| [server](../../src/ops/platform/server.py), [웹](../../src/ops/platform/web) | 접속 코드·세션·허용 조회. 오류/미지원/empty 분리; 원격 키·원문 질문/응답을 브라우저에 전달하지 않음 |
| [자원 프로필](../../src/ops/platform/resource_profiles.py), [프로필 예제](../../src/ops/platform/profiles) | 수집 역할별 라벨·filesystem·scope 검증. 메모리 working set·노드 MemAvailable·GPU 원본 timestamp 구분 |
| [observations](../../src/ops/platform/observations.py), [alerts](../../src/ops/platform/alerts.py) | 수집/추적/경보 상태와 원래 오류를 분리. 외부 알림 발송·장기 부하 인수 미완료 |
| [Langfuse](langfuse.md) | Agent의 메타데이터 span·단계/ToolCall 연결. 인용 형식/의미 품질을 추적 성공으로 대체하지 않음 |

API/접속·CLI 명령은 [ops_platform 안내](../../src/ops/platform/README.md), 인프라 설치는 [관측 infra](../../src/infra/observability/README.md)에 한 번만 둔다. 알려진 호스트 포트 17100은 Ops 예시이며 QA 17110과 구분한다. 현재 값/수집량은 과거 표본 수치로 추정하지 않는다.

## 환경 발견과 출처

[Compose 발견](../../src/ops/platform/ops/discovery_compose.py), [Kubernetes 발견](../../src/ops/platform/ops/discovery_kubernetes.py), [갱신](../../src/ops/platform/ops/refresh_discovery.py)이 논리 서비스·소유/연결 관계를 검증한다. Compose는 project/service label·현재 Docker metadata·mount/연결을 사용한다. Kubernetes 구현/예제의 존재가 실제 cluster 배포·RBAC·관측·PVC 인수를 뜻하지 않는다. 선택한 환경과 허용 범위에서 발견하며 IP·현재 컨테이너 ID를 영구 업무 신원으로 사용하지 않는다.

managed field는 현재 provenance에서 확인한다. 사용자 재정의·부모 generation·원래/새 설정·owner를 비교하고 미완료 intent/pending/부분 갱신이 있으면 자동 새 세대를 만들지 않는다. 정상 변경 세대의 출처 이력을 보존하고 변화가 없으면 새 설정/이력 쓰기를 하지 않는다. 적용과 restart는 별도 효과이며 불확실 restart를 자동 반복/rollback하지 않는다. 원래 설정·실행 신원과 journal을 확인한 명시 복구만 허용한다.

resource profile은 수집 대상을 생성하는 장치가 아니다. 실제 exporter/Prometheus 라벨이 호환돼야 하며 없는 node/namespace/workload를 이름으로 추측하지 않는다. 하나의 endpoint·응답/페이지 한도와 metadata_complete를 클러스터 전체 목록/소유 관계 검증으로 확대하지 않는다. 인증/TLS·프록시와 외부 보호 설정의 공급은 배포 계약이다.

## 운영 검증의 경계

과거 서버는 제한된 4개 API/7개 자원 그룹, 화면·재시작 보존·장애 구분·경보 수신, Docker/session 격리 인수와 boot 전환을 확인했다. 실제 로그아웃/재부팅·장기 운영·Kubernetes 이식·외부 채널 발송은 미인수다. 자동갱신의 현재 제어 계약은 [worker 수명주기](worker-lifecycle.md), backup은 [별도 범위](../operations/backup-restore.md)를 따른다.
