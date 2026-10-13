# Ops 통합 이력과 원문 목차

2026-10-07 문서 정리에서 날짜별 안내와 완료 검사 결과를 한 [압축 이력](ops-evidence-202609-20261002.json.gz)으로 모았다. 각 기록의 UTF-8 원문·바이트 수·SHA-256·파일 모드를 그대로 보존했다. 실패/시간 초과/SKIP과 로컬 대역/실제 서버의 구분도 원문에 남아 있다. 아래 목차는 옛 링크의 도착점이며 당시 실행 명령을 현재 할 일로 안내하지 않는다.

현재 계약은 [관측/발견](../ops-platform/observability-discovery.md), [worker 수명주기](../ops-platform/worker-lifecycle.md), [백업 범위](../operations/backup-restore.md), [백업 실행기](../operations/ops-backup-execution.md)에 둔다. 현재 상태는 [상태표](../status.md), 최신 서버 복구 결과는 [별도 원본 JSON](../testing/ops-worker-recovery-closure-observed-20261002.json)이다.

| 이력 묶음 | 판정의 경계 |
|---|---|
| 관측·경보·조회 | 제한된 API/화면·재시작·장애 구분을 확인. 외부 알림·장기 부하·다중 사용자 인수와 별개 |
| 발견·worker | Compose/session/boot 후보·격리 unit/Docker 인수 후 운영 전환. 실제 재부팅·Kubernetes 실행은 미인수 |
| 유지보수·cold storage | 여러 실패·복구·native 격리 결과를 보존. 운영 수집 완료가 아님 |
| 암호화 세대·파일 복원 | 격리 native 16/16 PASS. 실제 운영 DB/image/application 복원은 미인수 |
| 운영 plan·측정·복구 | 운영 plan과 10월 2일 worker 복구는 통과. public measure 차단·운영 백업 보류 유지 |

프로젝트 루트에서 특정 원문을 읽을 때만 아래 명령을 사용한다. 압축 이력을 프로젝트에 풀어 중복 사본을 만들 필요가 없다.

```bash
python3 -B - <<'PY'
import gzip, hashlib, json
from pathlib import Path
p = Path('docs/history/ops-evidence-202609-20261002.json.gz')
records = json.loads(gzip.decompress(p.read_bytes()))['records']
name = 'docs/ops-platform/auto-refresh-boot-control-20260929.md'
row = records[name]
raw = row['content'].encode('utf-8')
assert len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256']
print(row['content'], end='')
PY
```

JSON 원문은 같은 방식으로 읽고 `json.loads(row['content'])`로 해석한다. `name`에는 아래 원래 경로를 지정한다. 압축 파일은 데이터이며 어떤 운영 명령도 실행하지 않는다.

## 날짜별 문서

<a id="agent-tracing-checkfix-20260926"></a>

- **O02 서버 검사 중단과 목록 검사 보완** · 원래 경로: `docs/ops-platform/agent-tracing-checkfix-20260926.md`

<a id="agent-tracing-o02-20260926"></a>

- **O02 Agent → Langfuse 메타데이터 추적** · 원래 경로: `docs/ops-platform/agent-tracing-o02-20260926.md`

<a id="alertmanager-restart-o04-20260927"></a>

- **O04 운영 Alertmanager 재시작 인수** · 원래 경로: `docs/ops-platform/alertmanager-restart-o04-20260927.md`

<a id="alerts-o04-20260927"></a>

- **O04 경보 — 최초 구현과 서버 인수** · 원래 경로: `docs/ops-platform/alerts-o04-20260927.md`

<a id="auto-refresh-boot-candidate-20260929"></a>

- **부팅 재진입·준비 대기 후보 구현 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-boot-candidate-20260929.md`

<a id="auto-refresh-boot-control-20260929"></a>

- **Ops 자동 갱신 운영 전환·복구 CLI — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-boot-control-20260929.md`

<a id="auto-refresh-boot-native-20260929"></a>

- **실제 격리 unit/Docker 부팅 정책 전환 인수 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-boot-native-20260929.md`

<a id="auto-refresh-boot-start-20260929"></a>

- **최초 boot 기동 어댑터 후보 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-boot-start-20260929.md`

<a id="auto-refresh-boot-transition-20260929"></a>

- **중지 상태 코드·정책 전환 후보 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-boot-transition-20260929.md`

<a id="auto-refresh-diagnostics-20260928"></a>

- **자동갱신 사전 진단 보완 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-diagnostics-20260928.md`

<a id="auto-refresh-management-plan-20260928"></a>

- **운영 자동갱신 수명주기 관리 설계 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-management-plan-20260928.md`

<a id="auto-refresh-observation-20260929"></a>

- **운영 자동갱신의 읽기 전용 연속 관찰 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-observation-20260929.md`

<a id="auto-refresh-persistence-check-20260929"></a>

- **지속 정책의 읽기 전용 진단 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-persistence-check-20260929.md`

<a id="auto-refresh-persistence-plan-20260929"></a>

- **자동갱신 지속 실행 전환과 장기 관찰 계획 — 2026-09-29** · 원래 경로: `docs/ops-platform/auto-refresh-persistence-plan-20260929.md`

<a id="auto-refresh-preflight-20260928"></a>

- **자동갱신 관리 사전 진단·상태 조회 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-preflight-20260928.md`

<a id="auto-refresh-production-start-20260928"></a>

- **운영 자동갱신 기동과 새 평가 확인 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-production-start-20260928.md`

<a id="auto-refresh-resume-fix-20260928"></a>

- **서비스 재개 경로 보완 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-resume-fix-20260928.md`

<a id="auto-refresh-service-20260928"></a>

- **systemd user 자동갱신 서비스 관리 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-service-20260928.md`

<a id="auto-refresh-service-acceptance-20260928"></a>

- **실제 user unit·Docker 격리 인수 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-service-acceptance-20260928.md`

<a id="auto-refresh-session-selection-20260928"></a>

- **자동갱신 사용자 세션 초기 경로 선택 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-session-selection-20260928.md`

<a id="auto-refresh-systemd-compat-20260928"></a>

- **자동갱신 loginctl 호환성과 사용자 세션 진단 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-systemd-compat-20260928.md`

<a id="auto-refresh-worker-20260928"></a>

- **자동갱신 worker 보호 처리 — 2026-09-28** · 원래 경로: `docs/ops-platform/auto-refresh-worker-20260928.md`

<a id="backup-capacity-20260929"></a>

- **관측 백업 목적지 발견·용량 계획 — 2026-09-29** · 원래 경로: `docs/ops-platform/backup-capacity-20260929.md`

<a id="backup-capture-core-20260930"></a>

- **비활성 파일 수집·암호화·격리 복원 코어** · 원래 경로: `docs/ops-platform/backup-capture-core-20260930.md`

<a id="backup-cold-storage-20260930"></a>

- **비활성 storage UID 읽기·중지 저장소 수집 후보** · 원래 경로: `docs/ops-platform/backup-cold-storage-20260930.md`

<a id="backup-cold-storage-cleanup-20261001"></a>

- **실패한 격리 저장소 run: 소유 자원 복구와 initializer 상태 확인** · 원래 경로: `docs/ops-platform/backup-cold-storage-cleanup-20261001.md`

<a id="backup-cold-storage-diagnostics-20260930"></a>

- **격리 저장소 인수: 선택 이미지 필드와 조회 진단 보완** · 원래 경로: `docs/ops-platform/backup-cold-storage-diagnostics-20260930.md`

<a id="backup-cold-storage-native-20260930"></a>

- **격리 native Docker 저장소 UID·암호화·유지보수 인수** · 원래 경로: `docs/ops-platform/backup-cold-storage-native-20260930.md`

<a id="backup-cold-storage-wait-20261001"></a>

- **격리 저장소 cleanup PASS와 disabled healthcheck 기동 대기 보완** · 원래 경로: `docs/ops-platform/backup-cold-storage-wait-20261001.md`

<a id="backup-db-sizes-20260929"></a>

- **관측 DB 사용량 조사와 boot 작업 마감 — 2026-09-29** · 원래 경로: `docs/ops-platform/backup-db-sizes-20260929.md`

<a id="backup-generation-20261001"></a>

- **암호화 백업 세대 통합 — 2026-10-01** · 원래 경로: `docs/ops-platform/backup-generation-20261001.md`

<a id="backup-generation-native-20261001"></a>

- **현재 결과 — 격리 세대 생성·파일시스템 복원 인수 완료 (2026-10-01)** · 원래 경로: `docs/ops-platform/backup-generation-native-20261001.md`

<a id="backup-maintenance-control-20260930"></a>

- **운영 유지보수 범위·복구 상태 읽기 전용 검토** · 원래 경로: `docs/ops-platform/backup-maintenance-control-20260930.md`

<a id="backup-maintenance-core-20260930"></a>

- **작업별 boot worker 유지보수 코어 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-core-20260930.md`

<a id="backup-maintenance-docker-diagnostics-20260930"></a>

- **유지보수 격리 Docker 준비 실패 진단 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-docker-diagnostics-20260930.md`

<a id="backup-maintenance-executor-20260930"></a>

- **검토된 운영 worker 유지보수 실행 어댑터** · 원래 경로: `docs/ops-platform/backup-maintenance-executor-20260930.md`

<a id="backup-maintenance-executor-native-20260930"></a>

- **새 유지보수 실행 어댑터 격리 native 인수** · 원래 경로: `docs/ops-platform/backup-maintenance-executor-native-20260930.md`

<a id="backup-maintenance-intent-fix-20260930"></a>

- **유지보수 복귀의 진행 중 평가·intent 관측 보완 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-intent-fix-20260930.md`

<a id="backup-maintenance-native-20260930"></a>

- **실제 사용자 서비스·Docker 격리 유지보수 인수 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-native-20260930.md`

<a id="backup-maintenance-network-recovery-20260930"></a>

- **유지보수 격리 네트워크 축소와 소유 자원 복구 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-network-recovery-20260930.md`

<a id="backup-maintenance-readback-fix-20260930"></a>

- **격리 유지보수 자원 조회 실패와 보완 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-maintenance-readback-fix-20260930.md`

<a id="backup-maintenance-template-fix-20260930"></a>

- **운영 유지보수 계획 Docker 템플릿 보완** · 원래 경로: `docs/ops-platform/backup-maintenance-template-fix-20260930.md`

<a id="backup-plan-contract-fix-20261001"></a>

- **운영 워커 복구 결과와 백업 계획 객체 계약 수정 (2026-10-01)** · 원래 경로: `docs/ops-platform/backup-plan-contract-fix-20261001.md`

<a id="backup-plan-snapshot-fix-20261001"></a>

- **백업 계획 두 snapshot의 비교와 마운트 순서 (2026-10-01)** · 원래 경로: `docs/ops-platform/backup-plan-snapshot-fix-20261001.md`

<a id="backup-preflight-20260928"></a>

- **관측 저장소 읽기 전용 사전 조사** · 원래 경로: `docs/ops-platform/backup-preflight-20260928.md`

<a id="backup-production-plan-20261001"></a>

- **운영 암호화 백업 목적지·키 준비 및 완전 세대 계획 (2026-10-01)** · 원래 경로: `docs/ops-platform/backup-production-plan-20261001.md`

<a id="backup-readiness-20260930"></a>

- **읽기 전용 백업 실행 준비 점검 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-readiness-20260930.md`

<a id="backup-readiness-observed-20260930"></a>

- **백업 실행 준비 서버 점검과 reserve 표시 보완 — 2026-09-30** · 원래 경로: `docs/ops-platform/backup-readiness-observed-20260930.md`

<a id="backup-restore-isolated-20261001"></a>

- **전체 백업 세대 격리 복원 — 2026-10-01** · 원래 경로: `docs/ops-platform/backup-restore-isolated-20261001.md`

<a id="backup-restore-plan-20260928"></a>

- **관측 데이터 백업·복원 계획과 O04 남은 범위** · 원래 경로: `docs/ops-platform/backup-restore-plan-20260928.md`

<a id="backup-workflow-20260929"></a>

- **관측 백업 실행·격리 복원 설계 — 2026-09-29** · 원래 경로: `docs/ops-platform/backup-workflow-20260929.md`

<a id="boot-worker-recovery-20261001"></a>

- **운영 boot 워커의 Docker metadata 복구 잠금 (2026-10-01)** · 원래 경로: `docs/ops-platform/boot-worker-recovery-20261001.md`

<a id="console-o03-20260927"></a>

- **O03 운영 조회 API와 통합 화면** · 원래 경로: `docs/ops-platform/console-o03-20260927.md`

<a id="discovery-20260928"></a>

- **기존 Compose 설치의 서비스·지표 자동 발견** · 원래 경로: `docs/ops-platform/discovery-20260928.md`

<a id="discovery-activation-20260928"></a>

- **발견 설정의 첫 운영 활성화** · 원래 경로: `docs/ops-platform/discovery-activation-20260928.md`

<a id="discovery-bootstrap-20260928"></a>

- **최초 설치의 발견 기반 Ops 연결 — 2026-09-28** · 원래 경로: `docs/ops-platform/discovery-bootstrap-20260928.md`

<a id="discovery-kubernetes-20260928"></a>

- **Kubernetes 서비스·지표 발견 adapter — 2026-09-28** · 원래 경로: `docs/ops-platform/discovery-kubernetes-20260928.md`

<a id="discovery-refresh-20260928"></a>

- **재발견·변경 감지와 출처 기반 설정 갱신** · 원래 경로: `docs/ops-platform/discovery-refresh-20260928.md`

<a id="discovery-time-fix-20260928"></a>

- **자동 발견 수집 시각 호환성 수정** · 원래 경로: `docs/ops-platform/discovery-time-fix-20260928.md`

<a id="docker-acceptance-20260928"></a>

- **격리 Docker Ops 최초 설치·재발견/갱신 검증 — 2026-09-28** · 원래 경로: `docs/ops-platform/docker-acceptance-20260928.md`

<a id="docker-first-scope-20260928"></a>

- **단일 노드 Docker 우선 — 현재 검증 범위와 작업 순서** · 원래 경로: `docs/ops-platform/docker-first-scope-20260928.md`

<a id="fault-isolation-o03-20260927"></a>

- **O03 관측 장애 격리 — 별도 프로세스 검증** · 원래 경로: `docs/ops-platform/fault-isolation-o03-20260927.md`

<a id="filesystem-coverage-20260927"></a>

- **O04 디스크 감시 범위 보완** · 원래 경로: `docs/ops-platform/filesystem-coverage-20260927.md`

<a id="gpu-o01"></a>

- **O01 GPU 자원 수집** · 원래 경로: `docs/ops-platform/gpu-o01.md`

<a id="infrastructure-o01"></a>

- **O01 — 호스트·Docker 자원 수집 인프라** · 원래 경로: `docs/ops-platform/infrastructure-o01.md`

<a id="lan-access-fix-20260928"></a>

- **Ops LAN 접속 주소 수정** · 원래 경로: `docs/ops-platform/lan-access-fix-20260928.md`

<a id="langfuse-restart-compare-20260926"></a>

- **Langfuse 재시작 비교 중단 — 진단과 기존 기준 재조회** · 원래 경로: `docs/ops-platform/langfuse-restart-compare-20260926.md`

<a id="langfuse-restart-o01"></a>

- **O01 Langfuse 재시작 후 고정 추적 보존** · 원래 경로: `docs/ops-platform/langfuse-restart-o01.md`

<a id="platform-api-o02-20260927"></a>

- **O02 플랫폼별 상태 API 연결** · 원래 경로: `docs/ops-platform/platform-api-o02-20260927.md`

<a id="portability-20260928"></a>

- **관측 조회의 환경 분리와 Kubernetes 확장 준비** · 원래 경로: `docs/ops-platform/portability-20260928.md`

<a id="restart-o03-20260927"></a>

- **O03 Ops 재시작·설정·캐시·세션 검증** · 원래 경로: `docs/ops-platform/restart-o03-20260927.md`

<a id="watch-acceptance-20260928"></a>

- **유한한 Docker foreground watch 검증 — 2026-09-28** · 원래 경로: `docs/ops-platform/watch-acceptance-20260928.md`

## 완료 검사·서버 관측 원문

<a id="ops-auto-refresh-boot-candidate-local-20260929"></a>

- **ops-auto-refresh-boot-candidate-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-candidate-local-20260929.json`

<a id="ops-auto-refresh-boot-candidate-verified-server-20260929"></a>

- **ops-auto-refresh-boot-candidate-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-candidate-verified-server-20260929.json`

<a id="ops-auto-refresh-boot-control-local-20260929"></a>

- **ops-auto-refresh-boot-control-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-control-local-20260929.json`

<a id="ops-auto-refresh-boot-guard-fix-local-20260929"></a>

- **ops-auto-refresh-boot-guard-fix-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-guard-fix-local-20260929.json`

<a id="ops-auto-refresh-boot-native-local-20260929"></a>

- **ops-auto-refresh-boot-native-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-native-local-20260929.json`

<a id="ops-auto-refresh-boot-native-observed-server-20260929"></a>

- **ops-auto-refresh-boot-native-observed-server-20260929 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-boot-native-observed-server-20260929.json`

<a id="ops-auto-refresh-boot-native-verified-server-20260929"></a>

- **ops-auto-refresh-boot-native-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-native-verified-server-20260929.json`

<a id="ops-auto-refresh-boot-plan-wait-local-20260929"></a>

- **ops-auto-refresh-boot-plan-wait-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-plan-wait-local-20260929.json`

<a id="ops-auto-refresh-boot-plan-wait-observed-server-20260929"></a>

- **ops-auto-refresh-boot-plan-wait-observed-server-20260929 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-boot-plan-wait-observed-server-20260929.json`

<a id="ops-auto-refresh-boot-production-observed-20260929"></a>

- **ops-auto-refresh-boot-production-observed-20260929 — 원문 result=production_transition_and_initial_start_passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-production-observed-20260929.json`

<a id="ops-auto-refresh-boot-start-local-20260929"></a>

- **ops-auto-refresh-boot-start-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-start-local-20260929.json`

<a id="ops-auto-refresh-boot-start-verified-server-20260929"></a>

- **ops-auto-refresh-boot-start-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-start-verified-server-20260929.json`

<a id="ops-auto-refresh-boot-transition-local-20260929"></a>

- **ops-auto-refresh-boot-transition-local-20260929 — 원문 result=passed_with_one_regression_environment_limitation** · 원래 경로: `docs/testing/ops-auto-refresh-boot-transition-local-20260929.json`

<a id="ops-auto-refresh-boot-transition-verified-server-20260929"></a>

- **ops-auto-refresh-boot-transition-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-boot-transition-verified-server-20260929.json`

<a id="ops-auto-refresh-diagnostics-local-20260928"></a>

- **ops-auto-refresh-diagnostics-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-diagnostics-local-20260928.json`

<a id="ops-auto-refresh-diagnostics-server-20260928"></a>

- **ops-auto-refresh-diagnostics-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-diagnostics-server-20260928.json`

<a id="ops-auto-refresh-observation-local-20260929"></a>

- **ops-auto-refresh-observation-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-observation-local-20260929.json`

<a id="ops-auto-refresh-observation-verified-server-20260929"></a>

- **ops-auto-refresh-observation-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-observation-verified-server-20260929.json`

<a id="ops-auto-refresh-persistence-check-local-20260929"></a>

- **ops-auto-refresh-persistence-check-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-persistence-check-local-20260929.json`

<a id="ops-auto-refresh-persistence-observed-server-20260929"></a>

- **ops-auto-refresh-persistence-observed-server-20260929 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-persistence-observed-server-20260929.json`

<a id="ops-auto-refresh-preflight-local-20260928"></a>

- **ops-auto-refresh-preflight-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-preflight-local-20260928.json`

<a id="ops-auto-refresh-preflight-server-20260928"></a>

- **ops-auto-refresh-preflight-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-preflight-server-20260928.json`

<a id="ops-auto-refresh-preflight-verified-server-20260928"></a>

- **ops-auto-refresh-preflight-verified-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-preflight-verified-server-20260928.json`

<a id="ops-auto-refresh-production-start-local-20260928"></a>

- **ops-auto-refresh-production-start-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-production-start-local-20260928.json`

<a id="ops-auto-refresh-production-verified-server-20260929"></a>

- **ops-auto-refresh-production-verified-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-production-verified-server-20260929.json`

<a id="ops-auto-refresh-resume-fix-local-20260928"></a>

- **ops-auto-refresh-resume-fix-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-resume-fix-local-20260928.json`

<a id="ops-auto-refresh-service-acceptance-local-20260928"></a>

- **ops-auto-refresh-service-acceptance-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-service-acceptance-local-20260928.json`

<a id="ops-auto-refresh-service-acceptance-observed-20260928"></a>

- **ops-auto-refresh-service-acceptance-observed-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-service-acceptance-observed-20260928.json`

<a id="ops-auto-refresh-service-acceptance-verified-server-20260928"></a>

- **ops-auto-refresh-service-acceptance-verified-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-service-acceptance-verified-server-20260928.json`

<a id="ops-auto-refresh-service-fixture-fix-local-20260928"></a>

- **ops-auto-refresh-service-fixture-fix-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-service-fixture-fix-local-20260928.json`

<a id="ops-auto-refresh-service-local-20260928"></a>

- **ops-auto-refresh-service-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-service-local-20260928.json`

<a id="ops-auto-refresh-service-observed-server-20260928"></a>

- **ops-auto-refresh-service-observed-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-service-observed-server-20260928.json`

<a id="ops-auto-refresh-service-verified-server-20260928"></a>

- **ops-auto-refresh-service-verified-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-service-verified-server-20260928.json`

<a id="ops-auto-refresh-session-env-server-20260928"></a>

- **ops-auto-refresh-session-env-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-session-env-server-20260928.json`

<a id="ops-auto-refresh-session-selection-local-20260928"></a>

- **ops-auto-refresh-session-selection-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-session-selection-local-20260928.json`

<a id="ops-auto-refresh-systemd-compat-local-20260928"></a>

- **ops-auto-refresh-systemd-compat-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-systemd-compat-local-20260928.json`

<a id="ops-auto-refresh-systemd-compat-server-20260928"></a>

- **ops-auto-refresh-systemd-compat-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-auto-refresh-systemd-compat-server-20260928.json`

<a id="ops-auto-refresh-worker-local-20260928"></a>

- **ops-auto-refresh-worker-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-worker-local-20260928.json`

<a id="ops-auto-refresh-worker-verified-server-20260928"></a>

- **ops-auto-refresh-worker-verified-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-auto-refresh-worker-verified-server-20260928.json`

<a id="ops-backup-capacity-local-20260929"></a>

- **ops-backup-capacity-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capacity-local-20260929.json`

<a id="ops-backup-capacity-observed-20260929"></a>

- **ops-backup-capacity-observed-20260929** · 원래 경로: `docs/testing/ops-backup-capacity-observed-20260929.json`

<a id="ops-backup-capacity-order-fix-local-20260929"></a>

- **ops-backup-capacity-order-fix-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capacity-order-fix-local-20260929.json`

<a id="ops-backup-capture-core-local-20260930"></a>

- **ops-backup-capture-core-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capture-core-local-20260930.json`

<a id="ops-backup-capture-core-package-20260930"></a>

- **ops-backup-capture-core-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capture-core-package-20260930.json`

<a id="ops-backup-capture-core-server-20260930"></a>

- **ops-backup-capture-core-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capture-core-server-20260930.json`

<a id="ops-backup-capture-core-verified-package-20260930"></a>

- **ops-backup-capture-core-verified-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-capture-core-verified-package-20260930.json`

<a id="ops-backup-cold-storage-cleanup-local-20261001"></a>

- **ops-backup-cold-storage-cleanup-local-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-cleanup-local-20261001.json`

<a id="ops-backup-cold-storage-cleanup-package-20261001"></a>

- **ops-backup-cold-storage-cleanup-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-cleanup-package-20261001.json`

<a id="ops-backup-cold-storage-cleanup-server-20261001"></a>

- **ops-backup-cold-storage-cleanup-server-20261001** · 원래 경로: `docs/testing/ops-backup-cold-storage-cleanup-server-20261001.json`

<a id="ops-backup-cold-storage-diagnostics-local-20260930"></a>

- **ops-backup-cold-storage-diagnostics-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-diagnostics-local-20260930.json`

<a id="ops-backup-cold-storage-diagnostics-package-20260930"></a>

- **ops-backup-cold-storage-diagnostics-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-diagnostics-package-20260930.json`

<a id="ops-backup-cold-storage-diagnostics-server-20260930"></a>

- **ops-backup-cold-storage-diagnostics-server-20260930** · 원래 경로: `docs/testing/ops-backup-cold-storage-diagnostics-server-20260930.json`

<a id="ops-backup-cold-storage-local-20260930"></a>

- **ops-backup-cold-storage-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-local-20260930.json`

<a id="ops-backup-cold-storage-native-local-20260930"></a>

- **ops-backup-cold-storage-native-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-native-local-20260930.json`

<a id="ops-backup-cold-storage-native-package-20260930"></a>

- **ops-backup-cold-storage-native-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-native-package-20260930.json`

<a id="ops-backup-cold-storage-native-server-20261001"></a>

- **ops-backup-cold-storage-native-server-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-native-server-20261001.json`

<a id="ops-backup-cold-storage-native-verified-package-20261001"></a>

- **ops-backup-cold-storage-native-verified-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-native-verified-package-20261001.json`

<a id="ops-backup-cold-storage-package-20260930"></a>

- **ops-backup-cold-storage-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-package-20260930.json`

<a id="ops-backup-cold-storage-server-20260930"></a>

- **ops-backup-cold-storage-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-server-20260930.json`

<a id="ops-backup-cold-storage-verified-package-20260930"></a>

- **ops-backup-cold-storage-verified-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-verified-package-20260930.json`

<a id="ops-backup-cold-storage-wait-local-20261001"></a>

- **ops-backup-cold-storage-wait-local-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-wait-local-20261001.json`

<a id="ops-backup-cold-storage-wait-package-20261001"></a>

- **ops-backup-cold-storage-wait-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-cold-storage-wait-package-20261001.json`

<a id="ops-backup-cold-storage-wait-server-20261001"></a>

- **ops-backup-cold-storage-wait-server-20261001** · 원래 경로: `docs/testing/ops-backup-cold-storage-wait-server-20261001.json`

<a id="ops-backup-db-sizes-local-20260929"></a>

- **ops-backup-db-sizes-local-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-db-sizes-local-20260929.json`

<a id="ops-backup-db-sizes-observed-server-20260929"></a>

- **ops-backup-db-sizes-observed-server-20260929 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-db-sizes-observed-server-20260929.json`

<a id="ops-backup-generation-local-20261001"></a>

- **ops-backup-generation-local-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-local-20261001.json`

<a id="ops-backup-generation-native-failure-server-20261001"></a>

- **ops-backup-generation-native-failure-server-20261001** · 원래 경로: `docs/testing/ops-backup-generation-native-failure-server-20261001.json`

<a id="ops-backup-generation-native-local-20261001"></a>

- **ops-backup-generation-native-local-20261001** · 원래 경로: `docs/testing/ops-backup-generation-native-local-20261001.json`

<a id="ops-backup-generation-native-package-20261001"></a>

- **ops-backup-generation-native-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-native-package-20261001.json`

<a id="ops-backup-generation-native-verified-package-20261001"></a>

- **ops-backup-generation-native-verified-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-native-verified-package-20261001.json`

<a id="ops-backup-generation-package-20261001"></a>

- **ops-backup-generation-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-package-20261001.json`

<a id="ops-backup-generation-receipt-fix-local-20261001"></a>

- **ops-backup-generation-receipt-fix-local-20261001** · 원래 경로: `docs/testing/ops-backup-generation-receipt-fix-local-20261001.json`

<a id="ops-backup-generation-receipt-fix-package-20261001"></a>

- **ops-backup-generation-receipt-fix-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-receipt-fix-package-20261001.json`

<a id="ops-backup-generation-server-20261001"></a>

- **ops-backup-generation-server-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-generation-server-20261001.json`

<a id="ops-backup-maintenance-control-local-20260930"></a>

- **ops-backup-maintenance-control-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-control-local-20260930.json`

<a id="ops-backup-maintenance-core-local-20260930"></a>

- **ops-backup-maintenance-core-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-core-local-20260930.json`

<a id="ops-backup-maintenance-core-server-20260930"></a>

- **ops-backup-maintenance-core-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-core-server-20260930.json`

<a id="ops-backup-maintenance-docker-diagnostics-local-20260930"></a>

- **ops-backup-maintenance-docker-diagnostics-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-docker-diagnostics-local-20260930.json`

<a id="ops-backup-maintenance-docker-observed-20260930"></a>

- **ops-backup-maintenance-docker-observed-20260930 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-backup-maintenance-docker-observed-20260930.json`

<a id="ops-backup-maintenance-executor-local-20260930"></a>

- **ops-backup-maintenance-executor-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-local-20260930.json`

<a id="ops-backup-maintenance-executor-native-local-20260930"></a>

- **ops-backup-maintenance-executor-native-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-native-local-20260930.json`

<a id="ops-backup-maintenance-executor-native-package-20260930"></a>

- **ops-backup-maintenance-executor-native-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-native-package-20260930.json`

<a id="ops-backup-maintenance-executor-native-server-20260930"></a>

- **ops-backup-maintenance-executor-native-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-native-server-20260930.json`

<a id="ops-backup-maintenance-executor-native-verified-package-20260930"></a>

- **ops-backup-maintenance-executor-native-verified-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-native-verified-package-20260930.json`

<a id="ops-backup-maintenance-executor-package-20260930"></a>

- **ops-backup-maintenance-executor-package-20260930** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-package-20260930.json`

<a id="ops-backup-maintenance-executor-server-20260930"></a>

- **ops-backup-maintenance-executor-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-server-20260930.json`

<a id="ops-backup-maintenance-executor-verified-package-20260930"></a>

- **ops-backup-maintenance-executor-verified-package-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-executor-verified-package-20260930.json`

<a id="ops-backup-maintenance-intent-fix-local-20260930"></a>

- **ops-backup-maintenance-intent-fix-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-intent-fix-local-20260930.json`

<a id="ops-backup-maintenance-intent-observed-20260930"></a>

- **ops-backup-maintenance-intent-observed-20260930 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-backup-maintenance-intent-observed-20260930.json`

<a id="ops-backup-maintenance-native-local-20260930"></a>

- **ops-backup-maintenance-native-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-native-local-20260930.json`

<a id="ops-backup-maintenance-native-observed-20260930"></a>

- **ops-backup-maintenance-native-observed-20260930 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-backup-maintenance-native-observed-20260930.json`

<a id="ops-backup-maintenance-native-server-20260930"></a>

- **ops-backup-maintenance-native-server-20260930** · 원래 경로: `docs/testing/ops-backup-maintenance-native-server-20260930.json`

<a id="ops-backup-maintenance-native-verified-package-20260930"></a>

- **ops-backup-maintenance-native-verified-package-20260930** · 원래 경로: `docs/testing/ops-backup-maintenance-native-verified-package-20260930.json`

<a id="ops-backup-maintenance-network-observed-20260930"></a>

- **ops-backup-maintenance-network-observed-20260930 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-backup-maintenance-network-observed-20260930.json`

<a id="ops-backup-maintenance-network-recovery-local-20260930"></a>

- **ops-backup-maintenance-network-recovery-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-network-recovery-local-20260930.json`

<a id="ops-backup-maintenance-plan-server-20260930"></a>

- **ops-backup-maintenance-plan-server-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-plan-server-20260930.json`

<a id="ops-backup-maintenance-plan-verified-package-20260930"></a>

- **ops-backup-maintenance-plan-verified-package-20260930** · 원래 경로: `docs/testing/ops-backup-maintenance-plan-verified-package-20260930.json`

<a id="ops-backup-maintenance-readback-fix-local-20260930"></a>

- **ops-backup-maintenance-readback-fix-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-readback-fix-local-20260930.json`

<a id="ops-backup-maintenance-template-fix-local-20260930"></a>

- **ops-backup-maintenance-template-fix-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-maintenance-template-fix-local-20260930.json`

<a id="ops-backup-maintenance-template-observed-20260930"></a>

- **ops-backup-maintenance-template-observed-20260930 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-backup-maintenance-template-observed-20260930.json`

<a id="ops-backup-plan-contract-observed-20261001"></a>

- **ops-backup-plan-contract-observed-20261001** · 원래 경로: `docs/testing/ops-backup-plan-contract-observed-20261001.json`

<a id="ops-backup-plan-contract-package-20261001"></a>

- **ops-backup-plan-contract-package-20261001 — 원문 result=local_checks_passed_server_corrected_plan_pending** · 원래 경로: `docs/testing/ops-backup-plan-contract-package-20261001.json`

<a id="ops-backup-plan-snapshot-observed-20261001"></a>

- **ops-backup-plan-snapshot-observed-20261001** · 원래 경로: `docs/testing/ops-backup-plan-snapshot-observed-20261001.json`

<a id="ops-backup-plan-snapshot-package-20261001"></a>

- **ops-backup-plan-snapshot-package-20261001 — 원문 result=local_checks_passed_server_pending** · 원래 경로: `docs/testing/ops-backup-plan-snapshot-package-20261001.json`

<a id="ops-backup-production-plan-package-20261001"></a>

- **ops-backup-production-plan-package-20261001 — 원문 result=local_checks_passed_server_pending** · 원래 경로: `docs/testing/ops-backup-production-plan-package-20261001.json`

<a id="ops-backup-readiness-local-20260930"></a>

- **ops-backup-readiness-local-20260930 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-readiness-local-20260930.json`

<a id="ops-backup-restore-local-20261001"></a>

- **ops-backup-restore-local-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-restore-local-20261001.json`

<a id="ops-backup-restore-package-20261001"></a>

- **ops-backup-restore-package-20261001 — 원문 result=passed** · 원래 경로: `docs/testing/ops-backup-restore-package-20261001.json`

<a id="ops-backup-restore-server-20261001"></a>

- **ops-backup-restore-server-20261001** · 원래 경로: `docs/testing/ops-backup-restore-server-20261001.json`

<a id="ops-backup-workflow-design-20260929"></a>

- **ops-backup-workflow-design-20260929 — 원문 result=design_recorded** · 원래 경로: `docs/testing/ops-backup-workflow-design-20260929.json`

<a id="ops-boot-worker-recovery-package-20261001"></a>

- **ops-boot-worker-recovery-package-20261001 — 원문 result=local_checks_passed_server_pending** · 원래 경로: `docs/testing/ops-boot-worker-recovery-package-20261001.json`

<a id="ops-discovery-activation-local-20260928"></a>

- **ops-discovery-activation-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-activation-local-20260928.json`

<a id="ops-discovery-activation-server-20260928"></a>

- **ops-discovery-activation-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-activation-server-20260928.json`

<a id="ops-discovery-bootstrap-local-20260928"></a>

- **ops-discovery-bootstrap-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-bootstrap-local-20260928.json`

<a id="ops-discovery-bootstrap-server-20260928"></a>

- **ops-discovery-bootstrap-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-bootstrap-server-20260928.json`

<a id="ops-discovery-kubernetes-local-20260928"></a>

- **ops-discovery-kubernetes-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-kubernetes-local-20260928.json`

<a id="ops-discovery-kubernetes-server-20260928"></a>

- **ops-discovery-kubernetes-server-20260928 — 원문 result=attention_required** · 원래 경로: `docs/testing/ops-discovery-kubernetes-server-20260928.json`

<a id="ops-discovery-local-20260928"></a>

- **ops-discovery-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-local-20260928.json`

<a id="ops-discovery-refresh-local-20260928"></a>

- **ops-discovery-refresh-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-refresh-local-20260928.json`

<a id="ops-discovery-refresh-server-20260928"></a>

- **ops-discovery-refresh-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-discovery-refresh-server-20260928.json`

<a id="ops-discovery-server-20260928"></a>

- **ops-discovery-server-20260928 — 원문 result=discovered** · 원래 경로: `docs/testing/ops-discovery-server-20260928.json`

<a id="ops-discovery-time-fix-20260928"></a>

- **ops-discovery-time-fix-20260928** · 원래 경로: `docs/testing/ops-discovery-time-fix-20260928.json`

<a id="ops-docker-acceptance-local-20260928"></a>

- **ops-docker-acceptance-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-docker-acceptance-local-20260928.json`

<a id="ops-docker-acceptance-server-20260928"></a>

- **ops-docker-acceptance-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-docker-acceptance-server-20260928.json`

<a id="ops-watch-acceptance-local-20260928"></a>

- **ops-watch-acceptance-local-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-watch-acceptance-local-20260928.json`

<a id="ops-watch-acceptance-server-20260928"></a>

- **ops-watch-acceptance-server-20260928 — 원문 result=passed** · 원래 경로: `docs/testing/ops-watch-acceptance-server-20260928.json`
