# Model 단일 서버 백업과 복원 절차

이 문서는 Model 전용 기존 도구의 절차다. 전체 프로젝트 또는 Ops 관측 백업의 완료 선언이 아니다. 실제 복원 인수는 미확인이다. [전체 백업 범위](backup-restore.md)를 먼저 확인한다.

## 포함 범위와 전제

| 항목 | 백업 내용 |
|---|---|
| PostgreSQL 16 | model_operations, model_management, model_training, model_deployment 전체 custom dump |
| MinIO | 설정된 모델·데이터셋·Job 로그 버킷의 현재 모든 객체, SHA-256·metadata·Content-Type·tag |
| 영속 볼륨 | training-outputs, gpu-admission, MLflow의 /mlflow 전체 |
| 설정·소스 | platform .env, 해석된 Compose 설정, model_platform 소스와 docs |
| 검증 정보 | 파일별 SHA-256·크기, DB 테이블별 행 수, 서비스 이미지 ID, 객체 매핑 |

백업에 HF credential 복호화 키와 DB/S3 자격증명, 실행 식별 기록이 들어간다. 코드는 디렉터리 0700·파일 0600을 요청하지만 실제 저장소가 이를 보장하는지는 별도 확인해야 한다. exFAT 등에서는 마운트 옵션에 따라 권한 처리가 달라질 수 있다. 현재 도구는 생성 뒤 실제 접근 권한을 검증하지 않으므로, 향후 데이터 백업을 실행하기 전에 권한 검사를 보완하고 검증한다. 소유자만 접근하는 별도 저장소에 보관한다. 보고서와 달리 실제 백업 묶음을 채팅에 첨부하지 않는다. 같은 디스크의 다른 폴더는 디스크 장애 복구용 사본이 아니다.

현재 기본 구성인 로컬 Docker socket, 보통의 local named volume, postgres:5432, PostgreSQL 16, MLflow SQLite, 버전 관리를 사용하지 않는 세 MinIO 버킷에 맞춘 도구다. 실제 컨테이너와 Compose 환경이 다르거나 원격 실행 overlay가 켜져 있으면 진행을 거절한다. versioning Enabled/Suspended 버킷은 과거 버전을 누락한 백업으로 성공 처리하지 않고 거절한다. 버킷이 없으면 빈 버킷으로 대체하지 않는다.

서빙 캐시, Docker 컨테이너 자체·이미지 바이너리, GC로 보관하지 않은 Docker 로그, Kafka 디스크, PostgreSQL 전역 role/ACL, MinIO IAM/버킷 정책/과거 객체 버전은 포함하지 않는다. 모델 플랫폼 전용 역할과 버킷 정책은 복원 환경에서 재구성한다. Kafka를 별도로 복원하지 않아도 되도록 미완료 Operation·미발행 Outbox·미전달 submission이 있으면 백업을 거절한다. DB와 admission의 동일 시점 기록을 함께 복원하며 일부만 골라 되돌리지 않는다.

PostgreSQL의 pg_dump는 DB 하나의 일관된 덤프를 생성한다. 여러 DB와 객체 저장소를 함께 맞추기 위해 이 도구는 플랫폼 쓰기를 중지하도록 요구한다. [PostgreSQL 16 pg_dump](https://www.postgresql.org/docs/16/app-pgdump.html)

## 1. 도구 준비와 사전 확인

Python 3.10 이상과 Docker CLI를 사용한다. audit/verify/clean-cache는 표준 라이브러리만 쓰고 S3 백업·복원 검사에는 boto3가 필요하다. 기존 학습 Conda 환경 대신 별도 venv에 설치한다.

```bash
cd ~/workspace/agent_project/model_platform
python3.12 -m venv ~/.venvs/model-platform-ops
~/.venvs/model-platform-ops/bin/pip install -r ops/requirements.txt
~/.venvs/model-platform-ops/bin/python tests/test_platform_ops.py -v
```

호스트에 Python 3.12가 없다면 설치된 3.10 이상 실행 파일로 venv를 만든다. 백업 대상 모델의 총크기·볼륨·DB와 최소 20 GiB 여유 공간을 고려한다. 도구의 공간 계산은 보수적인 사전 추정이며 이후 다른 프로세스가 공간을 사용할 가능성까지 예약하지는 않는다.

UI에서 모든 가져오기·학습·복구 완료를 기다리거나 지원 API로 취소하고, 모든 배포를 종료한다. Operation이 dispatching/waiting_resources 등에 남으면 기존 복구 절차로 처리한다. 백업 목적으로 DB 상태를 강제로 succeeded로 바꾸지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_operational_smoke.py \
  --report tests/results/operations/before-maintenance-20260910.json
```

이 명령은 완료/실패/취소 상태의 목록을 읽어 저장한다. 비교 중 다른 사용자가 모델·작업을 추가/삭제하지 않도록 유지보수 시간을 정한다. 미완료 작업이 있으면 거절하며 취소·삭제를 대신 수행하지 않는다.

## 2. 서비스 중지와 백업

다음 경로와 S3 포트는 실제 환경에 맞춘다. 목적지는 기존 agent_project 밖의 **아직 없는 새 디렉터리**여야 한다. 기존 백업에 이어 쓰지 않는다. S3 포트는 `docker port ops-minio1 9000/tcp`로 확인한다. 비밀번호를 URL이나 명령에 넣지 않으며 현재 Compose 환경에서 읽는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  PLATFORM_BACKUP_DIR=/mnt/backup/model-platform-20260910
  PLATFORM_S3_ENDPOINT=http://127.0.0.1:19000

  trap 'docker start ops-mlflow; docker compose --profile playground start operation management training deployment gateway playground reconciler gc dispatcher' EXIT
  docker compose --profile playground stop \
    playground gateway dispatcher reconciler gc deployment training management operation
  docker stop ops-mlflow

  ~/.venvs/model-platform-ops/bin/python ops/platform_ops.py backup \
    --destination "$PLATFORM_BACKUP_DIR" \
    --s3-endpoint "$PLATFORM_S3_ENDPOINT"
)
```

동적 Job/서빙 컨테이너는 Compose stop만으로 종료되지 않는다. 도구가 Docker 전체 목록과 DB를 다시 확인하며, 모든 관련 작업이 exited가 아니거나 볼륨을 쓰는 다른 컨테이너가 있으면 거절한다. 유지보수 중 PostgreSQL과 MinIO는 켜 둔다. 다른 프로그램에서 플랫폼 DB·버킷·볼륨을 직접 수정하지 않는다.

백업 중 서비스 재기동·버킷 내용 변경·짧게 끝난 파일 스트림·I/O 오류가 확인되면 실패한다. manifest가 없거나 verify를 통과하지 못한 디렉터리는 완료 백업이 아니다. 출력이 없다는 이유로 같은 경로에 새 백업을 겹쳐 실행하지 않는다. 한 파일이 크면 처리 시간이 길 수 있으며 버킷별 진행 메시지가 나온다.

`.env`의 credential 암호화 키를 백업 뒤 임의 재생성하지 않는다. 서비스가 재기동됐으면 목록을 다시 비교한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_operational_smoke.py \
  --compare tests/results/operations/before-maintenance-20260910.json \
  --report tests/results/operations/after-maintenance-20260910.json
```

Gateway가 아직 시작 중이면 준비를 확인하고 새 보고서 이름으로 다시 읽는다. 비교 실패가 나면 앞뒤 JSON으로 누락·상태 변화를 확인한다. 이력 비교가 모델 추론이나 모든 DB 열의 검증을 대체하지는 않는다.

## 3. 파일 검증과 격리 복원 검사

```bash
cd ~/workspace/agent_project/model_platform
~/.venvs/model-platform-ops/bin/python ops/platform_ops.py verify-backup \
  --backup /mnt/backup/model-platform-20260910

~/.venvs/model-platform-ops/bin/python ops/platform_ops.py restore-drill \
  --backup /mnt/backup/model-platform-20260910 \
  --report tests/results/operations/restore-drill-20260910.json
```

verify는 로컬 파일 집합·크기·SHA-256을 검사한다. restore-drill은 그 뒤 다음 작업을 수행한다.

1. 기존 서버에서 사용하던 PostgreSQL·MinIO·Playground **이미지 ID**가 로컬에 있는지 확인한다. 다른 태그를 pull하지 않는다.
2. 새 임시 PostgreSQL과 볼륨을 만들고 네 DB를 pg_restore --exit-on-error로 복원한다. public 테이블 집합·행 수를 비교하고 복원 중 제약조건 오류도 실패로 처리한다.
3. 새 임시 볼륨에 학습 출력·admission·MLflow를 풀고 파일별 SHA-256을 다시 비교한다. MLflow SQLite integrity_check도 수행한다.
4. 새 임시 MinIO에만 객체를 복사하고 전체 객체 바이트를 다시 읽어 크기·SHA-256·metadata·Content-Type을 검사한다. metadata·tag는 보존하고 multipart 업로드를 지원한다.
5. 이번 실행에서 만든 임시 컨테이너·볼륨만 정리하고 결과를 기록한다. 정리 실패도 PASS로 처리하지 않는다.

운영 ops-infra에 연결하지 않는다. 임시 PostgreSQL은 network none이며, 임시 MinIO는 일반 bridge와 127.0.0.1의 임의 포트를 사용한다. 원본 DB·버킷·볼륨을 복원 대상으로 받는 인자는 없다. 실제 운영 서비스는 이 검사 동안 계속 실행할 수 있다. 단, 복원 데이터 전체와 여유 공간이 Docker 디스크에 추가로 필요하다.

덤프 파일 해시 성공만으로 복원 성공을 선언하지 않는다. pg_restore는 실제로 스키마와 데이터를 재구성하는 단계다. [PostgreSQL 16 pg_restore](https://www.postgresql.org/docs/16/app-pgrestore.html)

현재 작성 환경에는 Docker/MinIO가 없어 위 실제 복원 검사는 서버 실행 대기다. 단위 테스트의 대역 성공과 실제 복원 성공은 [최종 검증표](../testing/results-summary-20260916.md#model-v1)에서 구분한다.

## 실제 장애 복구 순서

복원 검사는 운영 서버를 자동 전환하지 않는다. 실제 디스크/호스트 장애 시에는 다음 순서로 **빈 복원 환경**을 준비한다.

1. 원래 호스트의 Dispatcher·Job·원격 worker가 다시 쓰지 못하도록 종료 상태를 확인한다. 백업 검증과 복원 검사를 통과한 한 시점의 묶음을 선택한다.
2. configuration/source.tar의 소스와 platform.env를 준비하고, 별도 보관한 infra Compose·인증 설정으로 PostgreSQL 16·MinIO·MLflow 인프라를 구성한다. 자동으로 플랫폼 서비스를 시작하지 않는다. 백업에 기록된 이미지 버전을 확보한다.
3. 비어 있는 네 DB에 pg_restore --no-owner --no-acl --exit-on-error로 복원한다. 해당 서비스를 실행할 DB role의 소유권과 권한을 맞춘다. 기존 업무 DB 위에 --clean을 실행하지 않는다.
4. 동일 이름의 전용 S3 버킷을 만들고 manifest.objects의 key에 각 file을 업로드한다. object 파일명은 안전한 숫자이므로 로컬 경로를 그대로 S3 key로 쓰지 않는다. 전체 readback을 비교한다. 별도 IAM/정책을 다시 설정한다.
5. training-outputs·gpu-admission·MLflow의 빈 named volume에 같은 시점의 tar를 복원한다. source 서비스의 volume 이름/MLflow 실행 UID와 권한을 맞춘다. serving-cache는 새로 준비해도 되며 checkpoint/output/admission은 캐시로 취급하지 않는다.
6. Kafka는 전용 명령 topic/group을 갖춘 환경을 준비한다. 이 도구의 백업은 작업이 모두 terminal이고 Outbox가 발행된 시점만 허용한다. 오래된 메시지가 있더라도 terminal Operation의 실행 차단 계약을 유지한다.
7. Operation → Management·Training·Deployment → Gateway·Playground → Reconciler·GC·Dispatcher 순서로 켠다. GPU admission에 과거 미확인 claim이 있으면 정확한 실행 이력을 조사하며 파일을 비워 우회하지 않는다.
8. 목록/이력·데이터셋 readback·대표 모델의 새 배포/채팅/중지로 복구 결과를 확인한다. 새 호스트에서 과거 stopped 컨테이너 ID·임시 endpoint가 살아 있다고 간주하지 않는다. 보관되지 않은 Docker 로그는 자동 복원되지 않는다.

전체 호스트 장애 전환과 외부 백업 스케줄링은 실환경 정보·백업 목적지·보존 기간을 확정한 뒤 검증한다. 이 문서는 다른 업무의 공유 인프라를 덮어쓰는 자동 복원 명령을 제공하지 않는다. [Docker 볼륨 백업·복원](https://docs.docker.com/engine/storage/volumes/), [S3 GetObject](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/get_object.html)를 참고한다.
