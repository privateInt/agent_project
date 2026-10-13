# 저장 공간과 보존 정책

갱신: 2026-10-07. 프로젝트 소스/문서 정리와 서버 저장소 회수는 별개다. Ops 운영 plan·격리 생성/파일 복원은 확인했지만 실제 운영 측정/수집/DB·image·application 복원은 보류, backup_ready=false다. 이전의 “목적지 미선택·새 도구 서버 확인 대기”는 초기 조사 당시 상태이며 [현재 백업 범위](backup-restore.md)를 따른다.

## 10월 7일 서버 관측과 선택

| 사용자 제공 관측 | 해석/현재 기록 |
|---|---|
| Docker/flows DB는 `/media/simsys/SSD_EXT4`의 `/dev/sda2` 사용, 최초 가용 0 | 홈 루트 디스크 여유 확보와 다른 저장소. n8n liveness 200이어도 readiness/settings 503이었음 |
| build cache 104.5GB→0, 이후 외장 디스크 가용 31GB·후속 27GB | 당시 관측값, 실제 회수량은 cache 표시와 동일하다고 단정하지 않음. n8n readiness는 200으로 회복 |
| MinIO 4노드 healthy/online, 같은 장치·각 avail 약 26.9GB | 네 공간을 합산하지 않음. healthy는 쓰기 성공 보장 아님. Data 업로드 503의 개별 backend 원인은 미확정 |
| 동일 TXT job의 후속 명시 전송 HTTP 200·CPU 처리 완료 | 실패를 유지한 복구 성공. 현재 전체 저장소/부하 정상 판정은 아님 |
| pip/go cache·휴지통 정리, 홈 가용 약 200GB | 사용자 확인된 당시 결과. VS Code·모델 cache·환경·다른 사용자 홈은 일괄 삭제하지 않음 |
| `tachoi` 약 79GB, 이동 후보 nvme 가용 약 75GB | 해당 이동 완료 로그 없음. 이번 문서 작업에서 이동/삭제하지 않음 |

사용자는 Docker 이미지의 최근 **7일 보존**을 선택했다. `until=168h`는 daemon 시각 기준 **이미지 생성 시각** 필터이며 최근 사용 시각의 7일 보존이 아니다. 컨테이너 참조 이미지와 사용하지 않는 재빌드용/pinned 이미지를 구분해 검토한다. 자동 prune·volume 삭제는 등록하지 않았다. 이미지 정리 명령의 실제 삭제 결과/회수량은 제공된 로그에 없어 완료로 기록하지 않는다. [Docker 공식 image prune 설명](https://docs.docker.com/reference/cli/docker/image/prune/)을 2026-10-07 확인했다.

대상은 현재 Docker 단일 서버 LLM 플랫폼이다. UI 구성은 확정했으며 이 운영 도구를 위해 화면을 추가하지 않는다. 실제 서버 결과는 [최종 검증](../testing/results-summary-20260916.md#model-v1)에 기록한다.

## 현황 점검

```bash
cd ~/workspace/agent_project/model_platform
python ops/platform_ops.py audit \
  --report "tests/results/operations/audit-$(date -u +%Y%m%dT%H%M%SZ).json"
```

매번 새 보고서 이름을 사용한다. 기존 보고서를 덮어쓰지 않는다. Gateway GET·Docker inspect·볼륨의 파일 크기/파일시스템 잔여량을 확인한다. 볼륨 조회는 현재 Playground 이미지 ID를 사용하는 일회성 CPU 컨테이너에 볼륨을 읽기 전용으로 연결한다. 모델/GPU Job을 만들지 않는다. 파일이 많으면 점검에 시간이 걸릴 수 있다.

보고서의 warning은 가용 바이트 또는 inode 비율 10% 미만, critical은 5% 미만 또는 여유 공간 20 GiB 미만이다. 운영용 기본 기준이며 MinIO 쓰기 성공이나 다음 학습에 충분한 공간을 보장하지 않는다. 모델 크기와 학습 중간 사본에 필요한 용량도 별도로 고려한다. critical·조회 불가·서비스 비정상은 종료 코드 2 또는 1로 확인한다.

같은 파일시스템 위의 Docker 볼륨과 MinIO node1~4는 같은 잔여 공간을 표시할 수 있다. 네 값을 더하지 않는다. 이전 서버에서 확인된 네 MinIO 경로는 모두 /dev/sda2에 있었다. 논리적인 노드가 네 개여도 그 디스크 하나의 장애에 대한 독립적인 백업이 되지는 않는다.

## 2026-09-10 초기화 이력

2026-09-10 사용자는 현재 테스트 모델·데이터셋·학습 결과를 초기화하고 소스만 보존하기로 했다. 따라서 아래는 **데이터를 계속 보유할 때의 일반 운영 정책**이며 이번 테스트 데이터 백업을 요구하지 않는다. 초기화에서는 DB·객체·볼륨·대기 이벤트의 연결을 함께 정리해야 한다. [초기화 범위](source-preservation.md)와 [실행 절차](reset-test-data.md)를 참고하며 공용 PostgreSQL·MinIO·Kafka 전체나 다른 프로젝트 데이터는 삭제 대상에 포함하지 않는다.

2026-09-10 초기 audit은 공유 디스크 용량 기준에서 실패했고, 이후 초기화·기동·재기동의 공간 검사는 통과했다. 당시 GiB 수치를 현재 여유 공간으로 사용하지 않는다. 현재 용량은 위 읽기 전용 audit으로 확인하며 Docker의 회수 가능 표시를 모두 삭제 가능한 데이터로 해석하지 않는다.

## 기본 보존 정책

| 대상 | 현재 정책 | 정리 방법 |
|---|---|---|
| MinIO 모델 | 자동 TTL 없음; 명시적인 숨김/영구 삭제 구분 | DELETE는 숨김, 모델 purge는 모든 버전의 파일·종료 배포 캐시를 보호 조건 확인 후 삭제. [계약](../reference/catalog.md#파일-영구-삭제) |
| 데이터셋·학습 작업 파일 | 모델 purge 범위 밖에서 유지 | 데이터셋/작업별 보존·복구 정책을 따른다. 모델 삭제와 함께 자동 삭제하지 않음 |
| 학습 완료 output | 원격 결과 검증 전까지 반드시 유지 | 기존 output/release API가 원격 ready 결과를 다시 검증한 후 해당 output만 정리 |
| checkpoint | 현재 구현의 최근 두 완료 checkpoint 유지 | 재개를 위한 파일은 별도 보존. 임의 디렉터리 삭제/TTL 없음 |
| GPU admission | DB와 함께 영속 보존·백업 | claims.json 또는 볼륨을 수동 초기화하지 않음 |
| Model Job 컨테이너·로그 | Compose 기본 apply, 성공 1시간·실패/취소 7일 | 로그 원격 저장·검증 후 컨테이너 제거. 실제 추론 컨테이너는 제외 |
| Data Job 컨테이너 | Reconciler 기본 apply, 성공 1시간·실패 7일 | 종료·도메인 완료 확인 후 제거. stdout/stderr 별도 아카이브 없음. [GC 정책](gc.md) |
| 프로젝트 테스트 파일 | Model·Data·Knowledge 결과 합계 1 MB 미만 목표, 실제 입력 별도 | [자료·결과 보관](../testing/artifact-retention.md); DB/MinIO 객체 삭제와 별개 |
| 서빙 캐시 | 해당 배포의 재구성 가능한 사본 | 아래 점검된 오프라인 개별 정리 |
| MLflow | SQLite DB와 /mlflow 전체 유지 | 플랫폼 백업에 포함 |
| Docker build cache | 실행 데이터와 별개, 10월 7일 0B 관측 | 필요할 때 별도로 검토. 광범위한 system/volume prune을 운영 기본값으로 사용하지 않음 |

모델·데이터셋 artifact의 자동 TTL 삭제는 구현하지 않았다. 이미 적용한 종료 Job 컨테이너 GC와 사용자가 명시하는 파일 영구 삭제는 구분한다. 전체 artifact 참조 추적·보존 기간에 따른 자동 삭제는 [후속 작업](../roadmap.md)에 남긴다.

## 완료 출력 정리

실제 GPU 학습·업로드 전용 복구·복구 adapter 서빙은 검증했다. 선택적 release의 실환경 검증은 남아 있다. 정리가 필요할 때 **한 작업**을 선택해 [출력 복구 문서 4단계](../testing/training-output.md)의 GET 확인 후 release를 실행한다. 실패/409가 나오면 원격 검증과 현재 실행 상태를 확인하며 파일을 직접 지워 우회하지 않는다.

## 중지된 배포의 캐시 정리

이 명령은 API·DB·모델·로그·학습 output을 삭제하지 않고 serving-cache 안의 지정된 deployment UUID 디렉터리만 대상으로 한다. 해당 배포는 다시 시작하지 않는 기존 계약이므로, 추후 같은 모델 배포는 새 배포로 만들고 MinIO에서 다시 준비한다.

먼저 UI의 배포 종료로 stopped를 확인한다. [백업 절차](backup-restore.md)에 따라 모든 작업을 끝내거나 취소하고 플랫폼 서비스와 MLflow를 중지한 유지보수 시간에 실행한다. 다른 실행이나 불확실한 생성·미전달 작업이 있으면 정리를 거절한다. 서비스가 실행 중인 상태에서 캐시를 임의로 삭제하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
STOPPED_DEPLOYMENT_ID='실제 stopped 배포의 UUID'
python ops/platform_ops.py clean-cache \
  --deployment-id "$STOPPED_DEPLOYMENT_ID" \
  --report "tests/results/operations/cache-plan-$(date -u +%Y%m%dT%H%M%SZ).json"
```

위 명령은 계획만 기록한다. 보고서의 대상이 맞고 그 배포의 로컬 서빙 사본이 필요 없을 때 같은 ID에 명시적으로 적용한다.

```bash
python ops/platform_ops.py clean-cache \
  --deployment-id "$STOPPED_DEPLOYMENT_ID" --apply \
  --report "tests/results/operations/cache-apply-$(date -u +%Y%m%dT%H%M%SZ).json"
```

운영 도구는 관리 API의 대체 스케줄러가 아니다. 플랫폼과 함께 관리하는 데이터·볼륨만 점검하고, contextrag 등 다른 서비스의 GPU 접근이나 데이터를 변경하지 않는다. [GC 운영](gc.md), [백업·복원](backup-restore.md)을 함께 참고한다.
