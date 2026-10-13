# 완료 출력 보존·복구 검증

현재 상태: 작성 환경 Python 86개 PASS. 2026-09-10 서버 Go 테스트 92개·하위 사례 110개와 실제 PostgreSQL·Compose/Bake 검사 PASS. 테스트 실패·SKIP은 없다. 이미지 전환 뒤 실제 GPU 학습·완료 출력 보존·업로드 전용 복구·ready adapter 등록과 복구 adapter의 실제 vLLM 추론·멱등 재요청·중지까지 PASS했다. 핵심 경로 검증은 완료했고, 아래 4단계의 선택적 로컬 사본 release만 실환경 미검증으로 남아 있다.

## 1. 적용과 Go/DB 검사

**서버 로그에서 1·2·3단계와 4단계 서빙은 완료했다. 같은 검사·빌드·학습/복구/서빙을 반복할 필요 없다.** 아래 명령은 이후 재검증할 때 사용한다. 로컬 출력은 보존하며, 정리가 필요하면 4단계의 선택적 release 절차를 따른다.

압축파일을 `agent_project`에 업로드하고 그 디렉터리에서 바로 푼다. DB 테스트는 연결 DB 안에 고유 schema를 생성하고 종료 시 제거한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker build --target builder \
    -t model-platform-check:training-output .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:training-output \
    go test -count=1 -v \
      ./internal/trainingoutput ./internal/jobcontrol \
      ./dispatcher/... ./training/... ./gateway/... \
      ./jobs/training/axolotl ./tests/creation

  python tests/check_build_layout.py
)
```

파일 seal·손상·symlink 거절, 동시 writer, 원격 ready 응답 유실, 등록 전 검증, Job 종료 검사, REST 응답·키·입력 경계, 실제 PostgreSQL 복구 키 경합·DB rollback·서비스 재생성·원래 실패 보존·정리 거절을 확인한다. Docker/S3/Management의 복구 테스트 응답은 대역이며 실제 GPU/MinIO 경로는 아래에서 따로 검증한다. DB 변수가 없으면 통합 테스트가 SKIP되므로 PASS와 함께 SKIP 여부를 확인한다.

## 2. 학습 Job·서비스 이미지 교체

**2026-09-10 01:28:25–01:28:26 UTC 기동 로그로 완료를 확인했다.** 학습 Job 이미지와 서비스 이미지 3개가 빌드됐고 `model-training-outputs` 볼륨 생성, 세 서비스 Up, Training 복구 controller·Gateway 46개 handler·Dispatcher ready를 확인했다. 아래는 이후 이미지 교체가 필요할 때 사용하는 명령이다. 실제 새 Job의 보존 볼륨 사용은 3단계에서 검증한다.

현재 실행 중인 학습과 진행 중인 취소가 끝난 뒤 적용한다. 새 Job의 볼륨 연결을 보장하기 위해 Dispatcher는 마지막에 전환한다. Training·Gateway·Dispatcher 3개와 학습 Job 이미지가 변경 대상이다. Management·Operation·Deployment 이미지를 교체할 필요는 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker buildx bake --builder default -f docker-bake.hcl --load training-job
  docker compose build training gateway dispatcher

  docker image inspect model-platform-training-axolotl:launch-v1 \
    --format '{{index .Config.Labels "model-platform.training-output-preservation"}}' |
    grep -x '1'

  docker compose stop dispatcher
  docker compose up -d --no-build --no-deps --force-recreate training
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose up -d --no-build --no-deps --force-recreate dispatcher

  docker compose ps training gateway dispatcher
  docker compose logs --tail=40 training gateway dispatcher
)
```

Training의 `training output recovery controller started` 기동과 `model-training-outputs` 볼륨 생성 여부를 확인한다. Dispatcher는 보존 기능 라벨이 없는 학습 Job 이미지를 거절한다. 기존 학습 Job 컨테이너에는 새 볼륨이 소급 적용되지 않는다.

## 3. 실제 1 step 학습 → 보존 → 업로드만 복구

**2026-09-10 사용자 실행 출력에서 최종 PASS를 확인했다.** TrainingRun `937859fa-4ed3-4367-84f5-50c74d332333`의 완료 출력이 retained로 보존됐고, 요청별 업로드 실패 뒤 재학습 없이 ready adapter `7d81088d-4043-44c5-90eb-c431d9c2ce17`을 등록했다. 원래 실행 이력·컨테이너 집합·Operation 개수 보존과 같은 복구 키의 재요청 검사도 통과했다. 보고서는 `tests/results/training-output/recovery-20260910.json`이며 마지막 Dispatcher Started 출력이 있다. 이 실행은 완료했으므로 아래 학습/복구 명령을 반복할 필요 없다.

GPU 0을 새 1 step 학습에 사용할 수 있고, 로컬 출력·MinIO 결과를 저장할 디스크 여유가 있어야 한다. fixture는 **한 번만 학습**한다. 완료 출력 seal 직후 해당 TrainingRun에만 publication 실패를 주입한다. MinIO를 정지하거나 다른 학습의 업로드를 차단하지 않는다.

아래 명령은 잠깐 Dispatcher를 멈춘 상태에서 테스트 요청을 만들고 그 ID에만 오류를 적용해 다시 시작한다. `trap`은 종료 시 오류 주입 설정을 제거한 Dispatcher를 재생성한다. 기존 `.env` 파일은 편집하지 않는다. 테스트 도중 새 테스트 요청을 반복 생성하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  OUTPUT_REPORT=tests/results/training-output/recovery-20260910.json

  docker compose stop dispatcher
  trap 'MODEL_TRAINING_TEST_FAIL_PUBLICATION_RUN_ID= docker compose up -d --no-build --no-deps --force-recreate dispatcher' EXIT

  python tests/live_training_output_smoke.py \
    --prepare \
    --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
    --gpu-id 0 \
    --report "$OUTPUT_REPORT"

  OUTPUT_RUN_ID=$(jq -er '.training_run_id' "$OUTPUT_REPORT")
  MODEL_TRAINING_TEST_FAIL_PUBLICATION_RUN_ID="$OUTPUT_RUN_ID" \
    docker compose up -d --no-build --no-deps --force-recreate dispatcher

  python tests/live_training_output_smoke.py \
    --report "$OUTPUT_REPORT" --timeout 600
)
```

보고서는 생성 POST 전에 저장하고 재실행 시 동일 키를 사용한다. 기본 600초는 학습 실패 관찰과 복구 관찰 각각의 제한이다. timeout은 서버 작업을 취소하지 않는다. 이미 학습이 시작했다면 같은 보고서로 **prepare 없이** 관찰·복구를 이어간다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_training_output_smoke.py \
  --report tests/results/training-output/recovery-20260910.json \
  --timeout 600
```

아직 GPU 대기 중인 상태에서 첫 명령이 timeout됐다면 Job이 시작됐는지 먼저 확인한다. 오류 주입이 제거된 상태로 시작하면 정상 학습 성공으로 끝날 수 있으며, smoke는 이를 복구 검증 성공으로 인정하지 않는다. 그 경우 기존 성공 결과를 실패로 바꾸지 않는다.

최종 PASS 조건:

- 원래 TrainingRun/Operation은 `TRAINING_ARTIFACT_PUBLISH_FAILED`로 실패하고 Job은 exited다.
- 완료 출력의 manifest와 파일 수·크기가 보존된다.
- 같은 복구 키는 같은 `recovery_id`를 반환한다.
- 복구는 `succeeded`이며 같은 base/run/manifest의 ready adapter가 생긴다.
- 원래 실패 상태·결과·MLflow ID·Operation 실행 시각/세대가 유지된다.
- 해당 TrainingRun의 Operation은 여전히 하나이고 원래 Docker Job만 남는다.

이 테스트는 실제 학습 완료 후 업로드 전 오류를 주입한다. MinIO 디스크 부족 자체를 재현하는 테스트는 아니다. 부분 업로드·readback·등록 응답 유실·DB rollback은 Go 테스트에서 구분하여 검증한다.

## 4. 복구 adapter 서빙과 로컬 사본 정리

**복구와 서빙 smoke 모두 PASS했다.** adapter `7d81088d-4043-44c5-90eb-c431d9c2ce17`을 GPU 0에서 로드해 ready → Operation succeeded → adapter 이름으로 `Hello!` 응답 → stopped를 확인했다. 동일 요청 재전송과 중지 후 성공 이력 보존도 통과했다. 서빙 보고서는 `tests/results/serving/888e513a-ca53-4af8-8cb0-c1c258c31e3a.json`이다. 아래 서빙 명령은 재검증이 필요할 때만 사용한다.

중간 `RUNTIME_CREATE_UNCONFIRMED`는 후속 관찰에서 오류가 비워지고 ready로 진행했다. 생성 미확인의 원인은 이 로그만으로 확정하지 않는다. 현재 추론 컨테이너는 stopped이고 로그·캐시는 보존됐다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  OUTPUT_REPORT=tests/results/training-output/recovery-20260910.json
  OUTPUT_MODEL_ID=$(jq -er 'select(.passed == true) | .output_model_version_id' "$OUTPUT_REPORT")
  python tests/live_serving_smoke.py \
    --model-version-id "$OUTPUT_MODEL_ID" --gpu-id 0
)
```

선택적 로컬 사본 release는 아직 실환경 결과가 없다. 서빙은 통과했으므로 로컬 학습 사본이 필요 없으면 아래 정리를 명시적으로 실행할 수 있다. 원격 모델·완료 receipt·복구 이력·Job 로그는 유지한다. 이 요청은 해당 학습 output 사본만 정리하며 별도 서빙 캐시는 정리하지 않는다. MinIO 전체 readback 때문에 수 분 걸릴 수 있다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  OUTPUT_RUN_ID=$(jq -er '.training_run_id' tests/results/training-output/recovery-20260910.json)
  curl -fsS --max-time 310 -X POST \
    "http://127.0.0.1:13000/api/training-runs/$OUTPUT_RUN_ID/output/release" |
    jq -e 'select(.availability == "released")'
)
```

409나 검증 오류가 나면 실패한 삭제를 강제하지 않고 GET output과 서비스 로그를 확인한다. release 응답 유실은 같은 명령으로 재시도한다. `docker volume rm`이나 광범위한 prune을 대신 실행하지 않는다.

## 실제 장애의 수동 복구 요청

과거 임시 파일이 지워진 학습에는 사용할 수 없다. 새 보존 기능으로 생성한 학습 중 GET output이 retained이고 원래 Job이 종료된 경우에 사용한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  : "${TRAINING_RUN_ID:?set the retained failed training run ID}"
  : "${OUTPUT_RECOVERY_KEY:?set a request key and reuse it for uncertain replies}"
  curl -fsS -X POST \
    -H "Idempotency-Key: $OUTPUT_RECOVERY_KEY" \
    "http://127.0.0.1:13000/api/training-runs/$TRAINING_RUN_ID/output/retry" | jq .
  curl -fsS \
    "http://127.0.0.1:13000/api/training-runs/$TRAINING_RUN_ID/output" | jq .
)
```

복구가 failed로 확정됐으면 같은 키가 다시 실행하지 않는다. 저장소 오류 등을 해결한 뒤 새로운 `OUTPUT_RECOVERY_KEY`를 사용한다. HTTP 응답만 유실됐다면 기존 키를 유지한다. 이력은 DB에 남고 현재 응답은 [API 계약](../reference/training-output.md)을 따른다.
