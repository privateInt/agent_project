# 종료 작업 컨테이너 정리

Model GC는 로컬 Docker의 종료 Job을 관찰하는 별도 서비스다. [서비스 설정](../../src/platform/model/gc/docker-compose.yml), [구현](../../src/platform/model/gc), [검증 절차](../testing/gc.md)를 기준으로 관리한다. 2026-09-16 자동 정리 요청에 따라 이 프로젝트의 Compose 기본값은 `apply`, 성공 보존 기간은 `1h`다. Data는 기존 Reconciler에서 정리하며 아래 Data 절을 따른다. 후속 대화에서 사용자가 자동 정리 동작을 확인했다.

## 2026-09-18 실제 로그 확인

사용자 로그 `붙여넣은 텍스트 (1)(20260918-074449).txt`에서 GC 기동과 `apply`, 성공 보존 `1h`, 실패 보존 `168h`를 확인했다. 포함된 32회 cycle의 errors는 모두 0이었고 성공 import Job 두 개는 `removed`로 정리됐다.

| 컨테이너 ID 접두사 | Operation 상태 | GC 판정 |
|---|---|---|
| `d37b3689e1ac` | failed | archived_retained |
| `0318e857edde` | failed | archived_retained |

두 배포 시작 Job은 로그가 아카이브됐고 실패 보존 기간 안에 있어 남았다. `Exited(0)`은 프로세스 종료 코드이며 Operation의 성공을 뜻하지 않는다. 이 경로는 도메인 실패를 보고한 뒤 정상 종료할 수 있다. 기준 시각에서 7일이 지나면 다음 GC 검사에서 다른 삭제 조건과 함께 다시 판정한다. 정확한 제거 시각은 이 로그만으로 확정하지 않는다. 정책 변경·수동 삭제는 하지 않았다. `model-training`은 상시 API 서비스이므로 Job GC 대상이 아니다.

## 실행 모드

| `MODEL_GC_MODE` | 동작 |
|---|---|
| `dry-run` | Docker inspect와 Operation 조회로 대상·보존 기간만 확인한다. `docker logs`, MinIO 연결·업로드, 삭제를 하지 않는다. |
| `archive` | 종료 Job의 로그를 저장하고 검증한다. 보존 기간과 관계없이 컨테이너는 유지한다. |
| `apply` | 로그를 저장·검증하고, 보존 기간이 지난 컨테이너만 제거한다. |

신규 설치 후에는 [검증 절차](../testing/gc.md)의 dry-run 결과부터 확인한다. 서비스는 기본 5분 간격으로 한 번에 최대 20개를 검사한다. ID 순환 방식으로 오래된 후보도 처리하며, Job 하나의 기본 제한 시간은 2분이다. `--once`는 **한 배치**만 실행하고 종료한다.

## 수집·삭제 조건

다음 조건을 모두 만족해야 로그를 수집한다.

- 소유 label이 `operation-dispatcher`이고, 전체 container ID·이름·Operation ID·현재 dispatch token·execution type이 일치한다.
- Operation의 `dispatch_protocol=1`, 실행 승인 시각과 execution instance ID, `completed_at`을 확인한다. 최종 상태는 `succeeded`, `failed`, `cancelled` 중 하나다.
- 현재 지원하는 import, training, deployment.apply 또는 deployment.serve Job이며 resource type/ID와 요청 노드·Docker 네트워크가 일치한다.
- 컨테이너가 `exited`이고 running/paused/restarting이 아니며, 자동 제거·재시작 정책·재시작 횟수가 없다. 시작·종료 시각도 정상이다.
- TTY를 사용하지 않으며 Docker log driver가 `json-file`, `local`, `journald` 중 하나다.

구형 protocol 0, 미승인 Job, 과거 dispatch token, Operation을 조회할 수 없는 컨테이너, 기존 smoke fixture는 남긴다. 종료됐지만 Operation이 아직 `running`인 Job도 자동 삭제하지 않는다. 먼저 도메인 결과·Reconciler 관찰을 확인한다.

`deployment.serve` 시작 Job은 기존 수집 정책을 적용한다. 실제 추론 컨테이너는 owner=`deployment-serving`, 이름=`model-serving-*`이므로 Job GC에서 제외된다. [서빙 Stop API](../reference/serving.md)는 GPU 프로세스만 중지하고 해당 컨테이너·캐시를 유지한다.

삭제 시에는 **컨테이너 종료 시각과 Operation 완료 시각 중 늦은 시각**부터 성공 1시간, 실패·취소 168시간(7일)을 계산한다. 업로드 이후와 삭제 직전에 Docker·Operation을 다시 조회한다. MinIO에서 로그·메타데이터·manifest를 다시 읽어 크기와 SHA-256을 확인한다. 어느 단계든 실패하거나 상태가 달라지면 남겨 둔다.

삭제는 전체 container ID에 대한 `docker rm`만 사용한다. `--force`, `--volumes`, prune, 이미지 삭제를 사용하지 않는다. 실행 중 컨테이너를 강제 종료하는 옵션은 없다. [Docker 제거 옵션](https://docs.docker.com/reference/cli/docker/container/rm/)

동시에 여러 GC가 같은 Job을 봐도 동일한 객체 경로를 재사용한다. 먼저 삭제된 컨테이너는 다음 목록에서 사라지며, 삭제 응답을 확인하지 못한 쪽은 성공으로 기록하지 않는다. Docker inspect와 rm을 한 트랜잭션으로 묶을 수는 없으므로 GC 대상에 대한 수동 restart/삭제를 수집과 동시에 수행하지 않는다. 플랫폼 자체는 승인 후 Job을 재시작하지 않는다.

## 보존되는 내용

모델 결과물과 별도 버킷인 `model-job-logs`에 저장한다. `archive`/`apply` 최초 실행은 버킷이 없으면 생성한다. dry-run은 버킷을 생성하지 않는다. 모델 artifact 버킷과 같은 이름이면 시작을 거부한다.

객체 prefix는 다음과 같다.

```text
s3://model-job-logs/operations/<operation_id>/<dispatch_token>/<container_id>/<manifest_sha256>/
```

| 파일 | 내용 |
|---|---|
| `stdout.log` | Docker가 반환한 stdout, RFC3339Nano timestamp 포함 |
| `stderr.log` | Docker가 반환한 stderr, timestamp 포함 |
| `metadata.json` | Operation·resource·실행 식별자, 최종 상태·완료 시각, 이미지 ID, 시작·종료 시각, exit code, OOM 여부 |
| `manifest.json` | 각 파일 경로·바이트 수·SHA-256. 나머지 파일 검증 이후 마지막에 기록한다. |

모델 adapter의 manifest 계약과 별도인 `kind: job-logs` 계약이다. 재시도해도 내용이 같으면 같은 prefix를 사용하고 기존 파일을 덮어쓰지 않는다. 이미 존재하는 파일의 검증이 실패하면 수집을 중단한다. ETag만으로 성공 처리하지 않는다.

`docker logs --timestamps`로 수집하며 환경 변수·명령 인자나 `--details`를 추가하지 않는다. Job이 stdout/stderr에 출력한 내용 자체는 그대로 보존한다. **Docker가 이미 회전·삭제한 로그나 컨테이너 안의 별도 로그 파일·작업 디렉터리·checkpoint는 수집하지 않는다.** 따라서 전체 실행 기록의 영구 보장을 의미하지 않는다. [Docker logs 옵션](https://docs.docker.com/reference/cli/docker/container/logs/), [로그 수집 범위](https://docs.docker.com/engine/logging/)

기본 stdout+stderr 합계 제한은 128 MiB다. 이를 넘거나 수집 시간이 초과되면 로그를 잘라 성공 처리하지 않고 컨테이너를 유지한다. GC 컨테이너의 임시 파일은 처리 종료 시 제거한다. 필요하면 바이트 제한/시간 제한을 조정해 재실행한다.

현재 GC는 저장한 로그 객체를 자동 삭제하지 않는다. 모델 파일·학습 출력·MinIO 부분 업로드 객체의 GC는 별도 후속 범위다. 학습 로그의 REST 조회는 [Training 로그 API](../reference/training-control.md)에서 현재 컨테이너와 이 archive를 읽는다. 실행 승인을 받은 cancelled Job은 실패 Job과 같은 보존 기간을 사용한다. manifest가 없는 부분 업로드도 다음 동일 수집에서 재사용하며, 별도 정리는 아직 하지 않는다.

## 환경 변수

호스트 설정은 `src/platform/model/.env`에서 아래 `MODEL_` 변수를 사용한다. MinIO 연결 정보는 기존 `MODEL_MINIO_*`, 네트워크는 `AGENT_PROJECT_NETWORK`, 노드는 `MODEL_DISPATCHER_NODE_NAME`을 사용한다.

| 호스트 변수 | 기본값 |
|---|---|
| `MODEL_GC_MODE` | `apply` |
| `MODEL_GC_LOG_BUCKET` | `model-job-logs` |
| `MODEL_GC_SUCCESS_RETENTION` | `1h` |
| `MODEL_GC_FAILURE_RETENTION` | `168h` |
| `MODEL_GC_INTERVAL` | `5m` |
| `MODEL_GC_ITEM_TIMEOUT` | `2m` |
| `MODEL_GC_MAX_LOG_BYTES` | `134217728` |
| `MODEL_GC_BATCH_SIZE` | `20` |

duration은 `24h`, `5m` 같은 Go duration 표기이며 최소 1초다. 바이트 상한은 1 GiB, batch 상한은 1000이다. 잘못된 값은 시작 오류가 된다.

모드 전환은 `.env`에서 `MODEL_GC_MODE=archive` 또는 `apply`로 수정한 뒤 다음과 같이 GC만 재생성한다. 성공·실패 보존 기간은 컨테이너 보존 기간이며 MinIO 로그의 만료 시간이 아니다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose up -d --no-build --no-deps --force-recreate gc
docker compose logs --tail=80 gc
```

GC 서비스 로그는 JSON으로 `container_id`, `operation_id`, `status`, `action`, `archive_uri`, `note`를 출력한다. `would_archive_and_remove`는 계획일 뿐 저장·삭제 검증 성공을 뜻하지 않는다. `archived_retained`는 로그 보존 후 컨테이너 유지, `removed`는 검증·삭제 응답까지 확인한 경우다. `retained`/`error_retained`의 이유를 먼저 확인한다. 아카이브 prefix는 `archive_uri`로 찾는다.

필요한 컨테이너 하나만 점검하려면 아래와 같이 실제 **전체 64자리 ID**를 입력한다. `GC_MODE`는 일회성 컨테이너 내부 변수이며 이 예시는 삭제하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
GC_CONTAINER_ID='GC 로그에 표시된 전체 64자리 container_id로 교체'
docker compose run --rm --no-deps -e GC_MODE=dry-run gc \
  /app/gc --once --container-id "$GC_CONTAINER_ID"
```

## Data 자동 정리

[Data Reconciler](../../src/platform/data/reconciler/app/job_cleanup.go)가 상태 보정과 별도 루프에서 종료 작업을 정리한다. 새 상시 서비스는 추가하지 않았다. 자동화 테스트와 Streamlit 직접 실행 모두 같은 정책을 적용한다.

| 호스트 변수 | 기본값 |
|---|---|
| `DATA_JOB_CLEANUP_MODE` | `apply` (`dry-run`, `off`로 변경 가능) |
| `DATA_JOB_SUCCESS_RETENTION` | `1h` |
| `DATA_JOB_FAILURE_RETENTION` | `168h` |
| `DATA_JOB_CLEANUP_INTERVAL` | `5m` |

`data-platform.owner=dispatcher`인 `exited` Job 중 수집·브라우저 수집·정제만 검사한다. Operation이 최종 상태여야 하고 현재 실행 승인·token·resource·이미지·네트워크·실행/종료 시각이 일치해야 한다. API 조회 실패, 실행 중/재시작/과거 token/미승인/지원하지 않는 작업은 유지한다. 삭제 직전 같은 증거를 다시 조회한다.

성공 Operation이면서 exit 0·OOM 없음은 1시간, 실패·취소 또는 종료 상태 불일치는 7일 뒤 삭제한다. 기준 시각은 Docker 종료와 Operation 완료 중 늦은 시각이다. 최대 20개 후보를 5분마다 순환하므로 보존 기간 직후 반드시 즉시 삭제되는 것은 아니다. 실제 `docker rm`은 강제·볼륨 옵션을 사용하지 않는다.

Data는 삭제 시 작업 번호·최종 상태·종료 코드를 Reconciler 로그에 기록하며 **개별 Job의 stdout/stderr 전체를 별도로 보관하지 않는다.** 삭제 뒤 해당 컨테이너의 `docker logs`는 사용할 수 없다. 오류 원인 조사나 장시간 테스트 재개가 필요하면 보존 기간을 늘리거나 `DATA_JOB_CLEANUP_MODE=off`로 둔 뒤 Reconciler를 재생성한다. 검증 전 `--rm`으로 즉시 지우지 않아 기존 테스트의 컨테이너 종료·이미지 검사를 유지한다. 이미 보존 기간이 지나 제거된 컨테이너를 요구하는 과거 smoke `--resume`은 런타임 검사를 완료할 수 없으며, 새 검증을 시작하거나 제거 전에 확인한다.

원본/정리 결과·DB 상태·MinIO 객체·이미지·볼륨은 삭제하지 않는다. 실제 OCR 추론 컨테이너 `model-serving-*`와 API·Streamlit은 대상이 아니다.

## 이미 쌓인 컨테이너의 수동 정리

[cleanup_jobs.py](../../src/platform/data/ops/cleanup_jobs.py)는 종료 후 1시간 이상 지난 로컬 Data 수집/정제와 Model import/deployment Job을 대상으로 Docker·Operation을 두 번 확인한다. 기본은 목록 확인이며, 실패·취소는 명시적으로 포함한다. 학습 Job은 로그/출력 보존 정책을 가진 Model GC에 맡긴다.

```bash
cd ~/workspace/agent_project
python src/platform/data/ops/cleanup_jobs.py --include-failed
python src/platform/data/ops/cleanup_jobs.py --include-failed --apply
```

첫 줄은 조회, 둘째 줄은 실제 삭제다. 수동 삭제는 개별 컨테이너 로그를 따로 아카이브하지 않는다. `--platform data` 또는 `model`로 좁힐 수 있다. Data/Model Gateway 주소는 `--data-api`, `--model-api`로 지정한다. API에서 완료를 확인하지 못한 컨테이너는 `retained`로 남으며 `Exited (0)`만으로 삭제를 승인하지 않는다. 이 수동 도구는 공개 API만 사용하므로 현재 dispatch token 검증은 상시 GC의 내부 gRPC 검증과 구분한다. Model 단건 API에 완료 시각이 없으면 수동 보존 기간은 Docker 종료 시각으로 계산한다.

## 2026-09-16 검증 상태

로컬 Data Reconciler 검사 10개(하위 30개), Model GC 단위 검사 14개(하위 49개), 수동 정리 Python 검사 8개를 확인했다. 실제 서버 Docker 삭제와 Model MinIO 보관은 로컬에서 실행하지 않았다. 적용 패키지의 활성화 절차는 Data CPU fixture 삭제와 기존 Model Docker/MinIO 격리 검사를 통과한 뒤 두 정리 서비스만 재생성한다. fixture는 고유 ID를 사용하고 검사 후 정리한다. 2026-09-16 사용자가 후속 대화에서 동작을 확인했으며, 추가 서버 로그나 삭제 개수는 제공되지 않았다.
