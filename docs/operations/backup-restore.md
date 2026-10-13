# 백업과 복원의 현재 범위

이 프로젝트에는 **Model 전용 기존 백업 도구**와 **Ops 관측 계층의 신규 암호화 백업 작업**이 함께 있다. 대상과 검증 수준이 다르다. 전체 프로젝트 백업이 완료됐다고 표시하지 않는다. 2026-10-02 worker 복구 확인으로 이번 운영 범위를 마무리하고, 실제 운영 백업/복원과 추가 자동화는 사용자 결정으로 보류한다.

## 세 범위를 구분한다

| 범위 | 코드/대상 | 현재 판정 |
|---|---|---|
| Model 기존 도구 | `src/platform/model/ops/backup.py`, `restore_drill.py`: Model 4개 DB·모델/데이터셋/로그 버킷·training/admission/MLflow 볼륨·설정 | 구현과 대역 검사 근거. 실제 운영 백업/격리 복원 인수는 미확인 |
| Ops 관측 백업 | `src/ops/tools/backup_plan.py`, `src/ops/candidates/backup_*`: Prometheus·Alertmanager·Langfuse·Ops 상태/설정·관련 이미지/worker 기록 | 격리 생성/파일시스템 복원 PASS. 운영 plan PASS, 실행기 구현/오프라인 검사 완료. 운영 측정/수집/DB/image/application 복원은 보류 |
| 전체 플랫폼 복구 | Data 원본/canonical·Knowledge 카탈로그/색인/참조·Agent DB/키·n8n DB/키·공용 infra 등 | 단일 일관된 백업/복구 흐름과 전체 인수 근거 없음. 별도 후속 설계 |

## Ops에서 완료한 범위

- 운영 목적지·새 age 키 준비와 이후 worker 복구는 보존 서버 결과에서 통과했다.
- 격리 fixture의 암호화 세대 생성·실제 image export·actor/foreign UID 파일시스템 복원·독립 readback·정리는 16/16 PASS다.
- image archive 구조/config/layer 검사는 Docker load·이미지 기동 또는 DB/애플리케이션 복원 검증을 대신하지 않는다.
- 최신 운영 plan은 서버 PASS다. 과거 `BACKUP_GENERATION_DISCOVERY_CHANGED` 기록도 보존한다.
- 측정 기반 수집/명시 복구 실행기를 구현했다. 파일/lease/파이프와 Docker/systemd 대역으로 검사했고, 새 실행기의 실제 호스트 연결 인수는 남아 있다.
- 2026-10-02 자동갱신 worker의 별도 명시 복구가 PASS였다. 새 invocation의 정상 평가 1회·현재 설정 readback을 확인했다. 백업 수집/복원 성공이나 실제 재부팅 검증을 뜻하지 않는다.

근거: [준비](../testing/ops-backup-setup-worker-recovery-observed-20261001.json), [이전 worker 복구](../history/ops-evidence-catalog.md#ops-backup-plan-contract-observed-20261001), [최신 worker 복구](../testing/ops-worker-recovery-closure-observed-20261002.json), [격리 native 결과](../testing/ops-backup-generation-native-server-20261001.json), [운영 plan PASS](../testing/ops-backup-plan-passed-server-20261002.json), [실행기 계약](ops-backup-execution.md).

## 아직 완료하지 않은 범위

아래는 보류한 후속 범위이며 현재 실행 순서가 아니다. 재개할 때 다음 과제 하나의 범위와 인수 기준을 정한다.

1. 검토된 유지보수 구간의 크기 조사/worker 복귀 연결 구현, 서버 조사와 예약 공간 확인. 현재 public `measure`는 `BACKUP_MEASURE_MAINTENANCE_REQUIRED`로 차단한다.
2. 새 실행기의 native Docker/systemd 연결 인수. 소스와 과거 격리 native 근거를 재사용해 추가 연결 범위만 검증한다.
3. 실제 애플리케이션 쓰기 중단/정상 종료 조건, 개인키 별도 보관, 최초 운영 세대 생성.
4. 그 실제 세대의 별도 환경 DB 복원·이미지 import·서비스 기동·원본 비교.
5. 보관/주기/외부 사본과 전체 업무 플랫폼의 복구 범위 확장.

현재 홈 목적지는 첫 로컬 복구용이다. 원본 디스크 장애에 대한 독립 사본이나 개인키의 별도 보관이 확인된 것은 아니다. 이 제한은 새로운 정책이 아니라 기존 준비 결과의 미확인 범위다. `backup_ready=false`를 유지한다.

## 절차 문서

- 현재 결정과 후속 논의: [로드맵](../roadmap.md), [Ops host 도구](../../src/ops/tools/README.md).
- Ops 계약: [운영 준비/계획](../history/ops-evidence-catalog.md#backup-production-plan-20261001), [snapshot 비교 보완](../history/ops-evidence-catalog.md#backup-plan-snapshot-fix-20261001), [세대 생성](../history/ops-evidence-catalog.md#backup-generation-20261001), [격리 복원](../history/ops-evidence-catalog.md#backup-restore-isolated-20261001).
- 기존 Model 절차: [Model 백업과 복원](model-backup-restore.md). 도구의 범위와 인수 한계를 보존해 별도 문서로 분리했다.

문서 적용은 백업·복원·서비스 중지 명령을 실행하지 않는다. 과거 날짜별 실행 예시는 현재 서버에서 반복 실행할 지시가 아니다.
