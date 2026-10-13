# 관측 인프라

호스트·컨테이너·GPU 지표를 수집하는 Prometheus·Node Exporter·cAdvisor·GPU collector와 Alertmanager 구성이다. 관측 도구의 기동/저장소는 infra가 관리하고 Ops는 API로 조회한다.

현재 기본/GPU 수집, 재시작 보존, 경보·파일시스템 감시에는 과거 서버 인수 근거가 있다. 완료 범위와 현재 백업 단계는 [Ops 상태](../../docs/ops-platform/README.md)에서 관리한다.

## 운영 문서

- [기본 수집 설치/저장/보관](../../docs/history/ops-evidence-catalog.md#infrastructure-o01)
- [GPU 수집과 전환](../../docs/history/ops-evidence-catalog.md#gpu-o01)
- [경보 구성](../../docs/history/ops-evidence-catalog.md#alerts-o04-20260927)
- [파일시스템 감시](../../docs/history/ops-evidence-catalog.md#filesystem-coverage-20260927)
- [서버 경보 확인](../../docs/history/ops-platform-202609-20261001.md#alerts-verified-20260927)
- [백업 범위](../../docs/operations/backup-restore.md)

최초 설치 시 프로젝트 루트의 `infra/ops/up.py --observability-only`, GPU 추가 시 `--gpu-only`가 해당 선택 그룹을 처리한다. 이미 인수한 서버에서는 문서 갱신을 이유로 재기동/수집/재시작 검사를 반복하지 않는다. 정확한 준비 조건과 영향은 위 운영 문서에서 확인한다.

Kubernetes adapter는 일부 구현돼 있으나 이 프로젝트의 Kubernetes 실제 배포/전환은 보류다. Docker의 검증 결과를 Kubernetes 인수로 확대하지 않는다.
