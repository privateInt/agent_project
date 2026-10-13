# 학습 취소·로그 API 검증

작성 환경 Python 3.12에서 관련 Python 테스트 **43개(새 smoke 7개 + 기존 36개)**가 통과했다. 2026-09-10 사용자 서버에서는 **Go 테스트 169개·하위 사례 164개가 13개 테스트 패키지에서 PASS**했고 Compose/Bake 구성 검사도 통과했다. 새 Go 테스트 19개가 모두 포함됐으며 RUN/PASS 이름 333개가 일치하고 실행된 테스트에 실패·SKIP은 없다.

후속 로그에서 **2번 서비스 6개 교체와 정상 기동도 확인했다.** 기존 Job의 누락된 `execution_node` 라벨로 발생한 503은 호환 수정 후 해결됐다. `internal/jobcontrol` Go 7개·하위 사례 37개와 Training 재배포, **3번 기존 학습 로그 조회·4번 대기 작업 취소·5번 실행 중 GPU Job 취소·6번 MLflow 종료·7번 보관 로그 조회까지 모두 PASS**했다. 이 범위로 로컬 단일 서버 학습 취소·로그 조회 검증을 완료한다. 아래 명령은 추후 회귀 검증용이며 지금 반복할 필요 없다. 현재 후속 순서는 [프로젝트 상태](../status.md)를 따른다. 자세한 근거는 [개발 이력](../history/model-platform-202609.md#2026-09-10-01-training-control)에 있다.

## 이전 Job 로그 호환 수정 — 적용·검증 완료

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

```bash
cd ~/workspace/agent_project/model_platform

(
  set -euo pipefail
  docker build --target builder -t model-platform-check:legacy-training-logs .
  docker run --rm model-platform-check:legacy-training-logs \
    go test -count=1 -v ./internal/jobcontrol

  docker compose build training
  docker compose up -d --no-build --no-deps --force-recreate training
  docker compose logs --tail=30 training
)

python tests/live_training_control_smoke.py \
  --training-run-id 143181ab-26cc-4f01-adb5-78b345fb7eeb
```

이번 코드는 DB·protobuf·GPU 예약 형식을 변경하지 않았으며 Training만 교체했다. 단위 테스트는 fake Docker HTTP와 실제 임시 guard 파일을 사용한다. 실제 읽기 전용 로그 smoke의 최종 PASS를 확인했으며 보고서는 `tests/results/training-control/b2bb4ca5-e593-4406-aa18-a894ab33415a.json`이다. JSON 자체는 첨부되지 않아 source·byte 수는 확인하지 않았다. 이후 4번 미시작 취소와 5번 실행 중 취소도 통과했다. 진행 중 진단에 필요한 실패 보고서는 보존하고, 완료된 과거 파일은 요약 후 정리한다.

## 1. 적용·Go/DB 테스트 — 완료

```bash
cd ~/workspace/agent_project/model_platform

(
  set -euo pipefail
  docker build --target builder -t model-platform-check:training-control .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:training-control \
    go test -count=1 -v \
      ./internal/gpuadmission ./internal/jobcontrol \
      ./operation/app ./training/... ./gateway/app \
      ./dispatcher/... ./deployment/... ./reconciler ./tests/creation

  docker run --rm model-platform-check:training-control \
    go test -count=1 -v ./gc -skip '^TestLive(GCArchiveAndRemoval|TrainingLogArchiveFallback)$'

  python tests/check_build_layout.py
)
```

DB 테스트는 고유 schema의 REST·gRPC·PostgreSQL fixture를 사용하며 실제 학습·서빙 Job은 만들지 않는다. 취소/완료 경합, 전달 실패·Operation commit 후 Training rollback, 늦은 보고·재요청을 검사한다. guard는 실제 flock·파일과 fake Docker를, 로그는 fake HTTP·메모리 archive를 사용한다. 이 결과를 실제 GPU/MinIO 검증으로 표시하지 않는다. PostgreSQL 테스트가 SKIP이면 완료로 기록하지 않는다.

## 2. 서비스 6개 교체 — 완료

활성 학습·서빙이 없는 시점에 적용한다. **Operation·Training·Gateway·Dispatcher·Deployment·GC**를 교체한다. Deployment도 GPU 예약 파일의 취소 기록을 보존해야 하므로 공유 guard 코드를 함께 갱신한다. 기존 Reconciler·학습 Job·vLLM 이미지는 재빌드할 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build operation training gateway dispatcher deployment gc

  docker compose stop dispatcher training deployment
  docker compose up -d --no-build --no-deps --force-recreate operation
  docker compose up -d --no-build --no-deps --force-recreate training deployment gc
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose up -d --no-build --no-deps --force-recreate dispatcher

  docker compose ps operation training gateway dispatcher deployment gc
  docker compose logs --tail=40 operation training gateway dispatcher deployment gc
)
```

Training은 Docker socket과 기존 `model-gpu-admission` volume, Docker CLI를 사용한다. `training cancellation controller started`를 확인한다. GC 운영 모드는 기존 환경 설정을 유지한다. API는 body 없는 POST만 허용한다.

## 3. 기존 성공 학습 로그 — 읽기 전용·완료

다음 ID는 앞서 성공한 Dataset 학습이다. 해당 Job 로그가 보존되어 있다면 생성이나 GPU 실행 없이 확인한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_training_control_smoke.py \
  --training-run-id 143181ab-26cc-4f01-adb5-78b345fb7eeb
```

`source=docker` 또는 `archive`를 보고서에 남긴다. 로그가 없으면 PASS로 넘기지 않는다. 보고서에는 로그 원문 대신 source·반환 byte 수만 기록한다. 원문은 다음 GET으로 조회할 수 있다.

```bash
curl -fsS \
  'http://127.0.0.1:13000/api/training-runs/143181ab-26cc-4f01-adb5-78b345fb7eeb/logs?tail_lines=20&max_bytes=16384' |
  jq '{source,availability,truncated,stdout,stderr}'
```

## 4. 대기 작업 취소 — GPU 실행 없음·완료

사용자 서버의 `--mode queued --timeout 600` 실행이 PASS했다. TrainingRun·Operation cancelled와 빈 cancellation error를 관찰했고, 실행 승인 없음·컨테이너 없음·취소/생성 재요청의 멱등성 검사도 통과했다. trap에 따른 Dispatcher 재기동까지 출력으로 확인했다. 보고서는 `tests/results/training-control/2a1c682d-6bfc-4506-9db5-04d8b472b0e8.json`이다. 아래 명령은 추후 회귀 검증용으로 남기며 지금 반복하지 않는다.

Dispatcher를 잠시 정지한 상태에서 새 fixture를 등록하고 취소한다. 다른 요청의 전달도 잠시 지연된다. trap은 smoke 성공·실패와 관계없이 Dispatcher를 다시 기동한다. smoke는 Dispatcher가 exited인지 먼저 검사하고 현재 실행 중인 기존 작업을 취소하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose stop dispatcher
  trap 'docker compose start dispatcher' EXIT

  python tests/live_training_control_smoke.py \
    --mode queued \
    --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
    --gpu-id 0
)
```

TrainingRun·Operation cancelled, 실행 승인 없음·컨테이너 없음, 취소 시각 보존, 원래 학습 생성 키 재요청 시 동일 ID를 확인한다. fixture 메타데이터는 삭제하지 않는다.

## 5. 실행 중 Job 취소 — 실제 GPU·완료

사용자 서버에서 `axolotl_training`과 실행 중 컨테이너를 관찰한 뒤 TrainingRun·Operation cancelled, 컨테이너 exited·로그 조회·취소/생성 재요청 검사가 모두 PASS했다. 보고서는 `tests/results/training-control/fd2a8a8e-068c-4d17-b835-93b99fb484f8.json`이다. cancellation error는 비어 있었고 후속 6번 GET에서 MLflow KILLED·종료 시각도 확인했다. 아래 명령은 완료 기록으로 남기며 지금 반복하지 않는다.

위 검증이 통과하고 GPU 0이 비어 있을 때 실행한다. synthetic 데이터로 최대 100 step fixture를 만들고 `axolotl_training` 단계와 running 컨테이너를 관찰한 뒤 즉시 취소한다. 학습 품질·완료 step 검증이 아닌 **실행 중 학습 Job 중지 검증**이다. 외부 GPU 접근 컨테이너를 자동 중지하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_training_control_smoke.py \
  --mode running \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --gpu-id 0 \
  --timeout 600
```

관찰 대기 기본값은 단계별 600초다. 이미지/모델 첫 준비가 오래 걸리면 `--timeout 1800`을 지정한다. 이 값은 테스트 관찰 시간이며 Job 실행 제한이나 강제 삭제 타이머가 아니다. smoke 실패 시 자신이 만든 fixture에 취소를 요청하고 해당 결과를 기록한다. 실패 출력만으로 중지 완료라고 판단하지 않는다.

각 실행은 `src/platform/model/tests/results/training-control/<UUID>.json`에 요청 키·ID·결과를 저장한다. 응답 유실이나 대기 만료 후 새 fixture를 만들기 전에 같은 보고서로 이어 확인한다.

```bash
python tests/live_training_control_smoke.py \
  --resume tests/results/training-control/REPORT_UUID.json \
  --timeout 600
```

queued 보고서는 Dispatcher를 정지한 상태에서 재개한다. 실행 중 취소 완료 후 Docker 컨테이너는 exited로 남고 로그가 조회되어야 한다. `CANCEL_TRACKING_SYNC_PENDING`이면 플랫폼 중지는 완료됐지만 MLflow 동기화만 남아 있다. Training GET의 해당 코드가 비워지는지 확인한다.

## 6. MLflow 종료 상태 — 읽기 전용·완료

사용자 서버 조회에서 TrainingRun `b273ba3d-d0c0-4f1a-86cc-c40ecd831bea`와 Operation `80a42926-5558-4d3d-9cc2-145b6eb4baa2`가 cancelled이고, MLflow run `377aff208d184f97a7efb38b11df00d3`은 KILLED로 확인됐다. end_time은 `1789001809477`(`2026-09-10T00:56:49.477Z`), cancellation error는 빈 값이며 읽기 전용 검사가 PASS했다.

다음 후속 패치는 테스트·진단 도구와 문서만 포함한다. 기존 서비스와 학습 Job 이미지는 교체하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform

python tests/inspect_cancelled_tracking.py \
  --report tests/results/training-control/fd2a8a8e-068c-4d17-b835-93b99fb484f8.json
```

보고서에서 TrainingRun·Operation ID를 읽고 플랫폼과 MLflow의 GET만 호출한다. 두 플랫폼 상태가 cancelled이고 취소 후속 오류가 없으며, 연결된 MLflow run이 종료 상태이고 `end_time`이 있으면 PASS한다. MLflow 주소 기본값은 `http://127.0.0.1:5000`이며 다르면 `--mlflow-api`를 지정한다. 결과는 선별한 ID·상태·종료 시각만 출력하며 원본 보고서와 MLflow를 변경하지 않는다.

현재 취소 구현은 활성 MLflow run을 KILLED로 갱신하고, 이미 FINISHED/FAILED/KILLED인 run은 보존한다. 따라서 이 도구는 세 terminal 상태를 허용하며 RUNNING/SCHEDULED는 실패로 처리한다. `end_time`은 숫자 또는 10진수 문자열의 양의 int64를 허용한다. [MLflow Get Run·RunInfo 계약](https://mlflow.org/docs/latest/api_reference/rest-api.html#get-run)을 참고한다. `CANCEL_TRACKING_SYNC_PENDING`이면 새 학습을 만들지 말고 controller 재시도 뒤 같은 조회를 실행한다.

## 7. 보관 로그 조회 — 격리 CPU·MinIO 통합 테스트·완료

실제 `TestLiveTrainingLogArchiveFallback`이 0.97초에 PASS했다. 컨테이너 제거 전 `source=docker`, 제거 후 `source=archive`와 stdout/stderr·실행 식별자 보존을 확인했다. 테스트 전용 버킷 `model-gc-test-cfdf291e-465f-4868-8af5-96d4cc546ca4`과 컨테이너 cleanup도 오류 없이 끝났다. 실패·SKIP은 없다.

기존 GC 통합 테스트에 Training 로그 reader 검증을 추가했다. 임의 UUID의 CPU 컨테이너와 `model-gc-test-<UUID>` 버킷 하나를 만들고 테스트 종료 때 정리한다. 실제 사용자 학습 컨테이너를 삭제하거나 상시 GC 모드를 전환하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker build --target gc-test -t model-platform-check:training-log-archive .
  python tests/run_gc_integration.py --training-logs
)
```

테스트 내부 Operation은 `training.run/training_run/cancelled` fixture다. Docker·MinIO와 Training의 실제 `internal/jobcontrol.Runtime.Logs`를 사용한다. 조회 source=docker → GC archive·manifest/SHA-256 readback → 보존 기간 판정·테스트 컨테이너 제거 → 조회 source=archive, 실행 식별자와 stdout/stderr가 일치해야 PASS한다. Operation 조회·시계는 fixture이므로 프로덕션 DB/Kafka/Training REST 통합 테스트와 구분한다. 이미 실행한 실제 REST 로그 검증과 함께 남은 reader-to-archive 연결을 확인하는 목적이다.

검사 이미지에 해당 테스트가 없거나 SKIP되면 runner도 실패한다. cleanup 오류 역시 실패다. 테스트 중에는 출력이 모였다가 완료 때 표시될 수 있다. 작성 환경에는 Go·Docker·MinIO가 없어 실행하지 못했으나 후속 사용자 서버 실행에서 위와 같이 PASS했다. Python 진단·기존 취소 smoke 테스트 14개는 작성 환경에서 PASS했다.

## 오류 확인

`CANCEL_CREATION_UNCONFIRMED`는 컨테이너를 한 번도 확인하지 못한 생성 예약이다. `CANCEL_STOP_UNCONFIRMED`는 Docker 관찰·중지·소유권 확인이 끝나지 않은 상태다. 둘 다 controller가 재시도하며 GPU 예약을 임의 삭제하지 않는다. `CANCEL_OPERATION_UNAVAILABLE`이면 Operation 서비스의 새 RPC와 기동 상태를 확인한다. `CANCEL_OPERATION_ALREADY_SUCCEEDED`는 도메인 결과 불일치이므로 해당 IDs와 retained Job을 검토한다.

```bash
docker compose logs --tail=80 training operation dispatcher
docker compose exec -T training cat /gpu-admission/claims.json
```

상시 GC가 dry-run이어도 7번의 격리 테스트를 실행할 수 있다. 실제 사용자 Job의 archive/apply 운영 전환은 [GC 검증](gc.md)과 운영 문서에서 별도로 다룬다. 사용자 Job을 로그 검증만을 위해 수동 삭제하지 않는다.

상태·오류·보존 정책은 [학습 제어 계약](../reference/training-control.md)을 따른다.
