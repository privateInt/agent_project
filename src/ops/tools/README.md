# Host 운영 도구

운영 관측과 백업 계획·유지보수·격리 인수 도구다. Ops 웹과 분리해 실행한다. 현재 상태와 진행 순서는 [프로젝트 상태](../../../docs/status.md), [로드맵](../../../docs/roadmap.md), [백업 범위](../../../docs/operations/backup-restore.md)에 둔다.

## 도구별 역할

| 도구 | 역할 | 상태 변경 범위 |
|---|---|---|
| `observe_refresh.py` | 기존 worker의 새 평가를 유한 시간 관찰 | 읽기 관찰 |
| `check_persistence.py` | session/boot 지속 조건 진단 | 읽기 진단 |
| `boot_control.py`, `recover_boot_worker.py` | boot 전환/기동·명시 복구 | 선택한 실제 worker 제어 가능 |
| `recover_changed_worker.py` | `OTHER_CONTAINER_CHANGED` failed worker의 검토된 one-shot 복구 | `plan`/`check`는 조회, 별도 `apply`는 같은 owned worker의 reset-failed/start·설정 readback |
| `plan_backup_capacity.py`, `measure_backup_databases.py`, `check_backup_readiness.py` | 용량·원본·준비 상태 조사 | 조회; DB 크기 도구에는 제한된 container exec 포함 |
| `maintenance_control.py`, `maintenance_execute.py` | 유지보수 계획·worker 중지/복귀/복구 연결 | 명시 실행 경로는 운영 상태 변경 가능 |
| `backup_setup.py` | age 도구·목적지·새 키 준비 또는 기존 준비 조회 | `prepare`는 파일/키 생성, `status`는 metadata 확인 |
| `backup_plan.py` | 운영 inventory를 암호화 세대 계약에 연결 | `plan`만 제공. 실제 백업/서비스 제어 없음 |
| `backup_execute.py`, `backup_measure.py` | 측정 adapter·측정 기반 검토·수집·명시 복귀/복구 | public `measure`는 유지보수 조사 연결 전 차단. `plan`은 metadata 조회, `capture`/`return`/`recover`는 명시 운영 제어 |
| `*_acceptance.py`, `check_backup_*.py`, `check_cold_storage.py` | 각 단계의 격리 검증 | 도구별 fixture·child·unit/Docker 사용 범위를 확인 |

현재 백업 생성/복원 코어는 `src/ops/candidates/backup_*`, `cold_storage_*` 등에 있고 격리 인수 도구에서 사용한다. 새 `backup_execute.py`에 운영 CLI를 연결하고 오프라인 검사를 수행했다. 새 연결의 실제 호스트 인수와 최초 운영 수집은 미확인이다. `backup_ready=false`를 유지한다. [계약](../../../docs/operations/ops-backup-execution.md)을 참고한다.

## 현재 결정

2026-10-02 명시 worker 복구가 PASS였고 새 invocation의 정상 평가 1회·현재 설정 readback을 확인했다. [서버 결과](../../../docs/testing/ops-worker-recovery-closure-observed-20261002.json)는 관찰 3.038초의 근거이며 실제 재부팅·장기 인수는 아니다. 이번 운영 범위는 복구 확인과 문서 정리로 마무리한다.

백업 유지보수 조사 연결·실제 크기 조사·운영 수집/복원과 추가 자동화는 사용자 결정으로 보류한다. public `measure`는 `BACKUP_MEASURE_MAINTENANCE_REQUIRED`로 차단한다. 복구 출력의 `next_step`은 보류된 후속 항목이며 지금 실행할 명령이 아니다. 다음은 [로드맵](../../../docs/roadmap.md)의 작업 경과·확장·품질 논의다.

기존 키·목적지·운영 plan과 격리 인수 근거를 재사용한다. 문서 반영을 이유로 recovery apply·`prepare`·start/resume·native 인수를 반복하지 않는다. 새 백업 실행기의 서버 측정/수집/복원은 미완료이며 `backup_ready=false`다.

## 상세 계약

- [boot와 활성 source binding](../../../docs/history/ops-evidence-catalog.md#auto-refresh-boot-control-20260929)
- [유지보수 제어](../../../docs/history/ops-evidence-catalog.md#backup-maintenance-control-20260930), [실행 어댑터](../../../docs/history/ops-evidence-catalog.md#backup-maintenance-executor-20260930)
- [세대 생성](../../../docs/history/ops-evidence-catalog.md#backup-generation-20261001), [격리 파일 복원](../../../docs/history/ops-evidence-catalog.md#backup-restore-isolated-20261001)
- [native 생성/파일 복원 결과](../../../docs/history/ops-evidence-catalog.md#backup-generation-native-20261001)
- [운영 준비/계획](../../../docs/history/ops-evidence-catalog.md#backup-production-plan-20261001), [최신 비교 보완](../../../docs/history/ops-evidence-catalog.md#backup-plan-snapshot-fix-20261001)
