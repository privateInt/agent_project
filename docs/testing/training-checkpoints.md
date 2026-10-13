# Checkpoint 보존·재개 검증

현재 상태: 작성 환경 Python 99개, 서버 Go **100개·하위 사례 116개**, 실제 PostgreSQL·Compose/Bake 검사 PASS. 이미지 전환 후 **실제 GPU 실패/취소 재개와 새 adapter 등록, 취소 재개 adapter의 vLLM 추론·멱등 재요청·중지까지 PASS**했다. 이 단계의 기능 검증은 완료했다. 아래 명령은 신규 적용·명시적 재검증용이며 이번 문서 갱신을 위해 반복할 필요 없다. 전체 식별자는 [개발 이력](../history/model-platform-202609.md#2026-09-10-03-training-checkpoints)에 있다.

## 완료한 검증 범위

| 항목 | 확인 결과 |
|---|---|
| 실패 후 재개 | 원래 failed 유지, 모델·optimizer·scheduler·RNG 복원, start=2/final=4/executed=2, 새 ready adapter |
| 취소 후 재개 | 원래 cancelled와 실제 Job 종료 확인, 같은 복원·진행·새 adapter 검사 |
| 재개 요청 | 새 TrainingRun·Operation·MLflow 연결, 같은 키 재현, 원본 checkpoint·이력 보존 |
| 실제 서빙 | 취소 재개 adapter `1a2a9116-9371-4f11-b636-c32dca3c8754`, vLLM ready·Hello!·멱등 재요청·stopped |

Gemma 3 1B·GPU 0·synthetic 데이터·4 step의 기능 검증이다. 장시간 안정성·모델 품질·등록 데이터셋을 지정한 재개의 실제 GPU 동작은 이번 결과로 완료 처리하지 않는다. 아래 report 경로는 이미 성공한 기록이다. 새 재검증을 의도하면 failed/cancelled 각각 새 report 파일명을 선택하고, 진행 중인 요청의 재관찰은 기존 파일을 그대로 사용한다.

## 1. 적용과 Go/DB 검사

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker build --target builder -t model-platform-check:training-checkpoints .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:training-checkpoints \
    go test -count=1 -v \
      ./internal/trainingcheckpoint ./internal/jobcontrol \
      ./dispatcher/... ./training/... ./gateway/... \
      ./jobs/training/axolotl ./tests/creation

  python tests/check_build_layout.py
)
```

Go 테스트의 Docker·Management는 대역이며 DB 테스트는 실제 PostgreSQL의 고유 schema를 사용한다. 실패와 SKIP 여부를 함께 확인한다. 선택한 패키지에는 별도 GC live fixture를 실행하지 않는다.

## 2. 이미지 교체

진행 중인 학습·취소가 끝난 뒤 학습 Job과 Training·Gateway·Dispatcher 3개 이미지를 교체한다. Dispatcher는 새 checkpoint 기능 라벨을 검사하므로 학습 Job을 먼저 빌드한다. 기존 결과물·현재 GPU 컨테이너를 정리하는 명령은 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail

  docker buildx bake --builder default -f docker-bake.hcl --load training-job
  docker compose build training gateway dispatcher

  docker image inspect model-platform-training-axolotl:launch-v1 \
    --format '{{index .Config.Labels "model-platform.training-checkpoints"}}' |
    grep -x '1'

  docker compose stop dispatcher
  docker compose up -d --no-build --no-deps --force-recreate training
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose up -d --no-build --no-deps --force-recreate dispatcher
  docker compose ps training gateway dispatcher
  docker compose logs --tail=40 training gateway dispatcher
)
```

Training 기동 시 resume_spec JSONB 열이 추가된다. 원래 학습 결과와 상태는 변경하지 않는다. checkpoint 미사용 요청의 기존 생성 fingerprint는 유지한다.

## 3. 2/4 step 실패 후 나머지 2 step 재개

GPU 0과 모델·checkpoint 사본을 저장할 디스크 여유가 필요하다. 이 테스트는 새 학습 Job 2개를 순차 실행한다. 첫 Job은 4 step 중 2 step을 완료·보존한 직후 해당 요청에만 오류를 주입한다. 두 번째 Job은 checkpoint를 읽고 남은 2 step을 실행한다. 원래 실패 이력은 유지된다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  CHECKPOINT_REPORT=tests/results/training-checkpoints/failed-20260910.json

  docker compose stop dispatcher
  trap 'MODEL_TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID= MODEL_TRAINING_TEST_CHECKPOINT_MODE=failed docker compose up -d --no-build --no-deps --force-recreate dispatcher' EXIT

  python tests/live_training_checkpoint_smoke.py \
    --prepare --mode failed \
    --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
    --gpu-id 0 --report "$CHECKPOINT_REPORT"

  CHECKPOINT_RUN_ID=$(jq -er '.training_run_id' "$CHECKPOINT_REPORT")
  MODEL_TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID="$CHECKPOINT_RUN_ID" \
  MODEL_TRAINING_TEST_CHECKPOINT_MODE=failed \
    docker compose up -d --no-build --no-deps --force-recreate dispatcher

  python tests/live_training_checkpoint_smoke.py \
    --report "$CHECKPOINT_REPORT" --timeout 600
)
```

`AXOLOTL_TRAIN_FAILED`와 `MODEL_PLATFORM_CHECKPOINT_STOP_FIXTURE`는 이 첫 Job에 의도한 오류다. 최종 PASS에는 checkpoint hash·원본 연결, 원래 실패 이력, 새 학습/Operation/MLflow ID, 같은 재개 키의 멱등성, `start_step=2`, `final_step=4`, `executed_steps=2`, 모델·optimizer·scheduler·RNG 복원과 ready adapter를 검사한다.

600초는 부모 중단 관찰·자식 학습 관찰 각각의 제한이다. timeout은 서버 작업을 취소하지 않는다. 보고서를 유지하고 새 요청을 만들지 않는다. 이미 첫 Job이 시작했다면 다음으로 같은 요청을 관찰·재개한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_training_checkpoint_smoke.py \
  --report tests/results/training-checkpoints/failed-20260910.json \
  --timeout 600
```

아직 GPU 대기 중일 때 timeout되면 EXIT trap이 오류 주입 설정을 제거한다. 그 뒤 시작한 Job은 정상 완료될 수 있으며 smoke는 이를 재개 검증 성공으로 인정하지 않는다. 이 경우 해당 Job 상태를 먼저 확인하고 성공 결과를 실패로 바꾸지 않는다.

## 4. 취소 후 재개

3단계 통과 후 취소된 학습의 checkpoint 재개도 검증한다. 2 step 저장 직후 해당 요청만 대기시키고 테스트가 기존 cancel API를 호출한다. 실제 종료가 확인된 뒤 새 학습으로 재개한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  CHECKPOINT_REPORT=tests/results/training-checkpoints/cancelled-20260910.json

  docker compose stop dispatcher
  trap 'MODEL_TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID= MODEL_TRAINING_TEST_CHECKPOINT_MODE=failed docker compose up -d --no-build --no-deps --force-recreate dispatcher' EXIT

  python tests/live_training_checkpoint_smoke.py \
    --prepare --mode cancelled \
    --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
    --gpu-id 0 --report "$CHECKPOINT_REPORT"

  CHECKPOINT_RUN_ID=$(jq -er '.training_run_id' "$CHECKPOINT_REPORT")
  MODEL_TRAINING_TEST_STOP_AFTER_CHECKPOINT_RUN_ID="$CHECKPOINT_RUN_ID" \
  MODEL_TRAINING_TEST_CHECKPOINT_MODE=cancelled \
    docker compose up -d --no-build --no-deps --force-recreate dispatcher

  python tests/live_training_checkpoint_smoke.py \
    --report "$CHECKPOINT_REPORT" --timeout 600
)
```

이 경우 원래 TrainingRun/Operation은 cancelled로 유지된다. timeout이면 같은 cancelled 보고서를 --prepare 없이 다시 사용한다. 이미 시작한 fixture의 대기는 Dispatcher의 설정 해제만으로 끝나지 않으며 같은 smoke가 취소를 요청해야 한다.

## 5. 재개 결과 서빙

실패/취소 재개 결과를 확인한 뒤 선택한 보고서의 새 adapter로 추론을 확인한다. 결과를 합치거나 원래 adapter ID를 대신 사용하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  CHECKPOINT_REPORT=tests/results/training-checkpoints/cancelled-20260910.json
  CHECKPOINT_MODEL_ID=$(jq -er 'select(.passed == true) | .output_model_version_id' "$CHECKPOINT_REPORT")
  python tests/live_serving_smoke.py --model-version-id "$CHECKPOINT_MODEL_ID" --gpu-id 0
)
```

API·호환성·보존 정책은 [checkpoint 계약](../reference/training-checkpoints.md)을 따른다. 새 재검증 결과는 기존 성공 이력을 덮어쓰지 않고 별도 보고서로 남긴다.
