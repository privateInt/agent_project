# 모델 가져오기와 학습 테스트

이 문서는 실제 모델·학습 리소스를 생성하는 수동 E2E 절차다. [로컬 서비스](../operations/local.md)와 Job 이미지를 먼저 준비한다. 아래 학습 예제는 기존 synthetic fixture를 사용한다. 등록된 JSONL 버전으로 학습하려면 [데이터셋 검증](datasets.md)을 따른다.

## 공개 모델 import

작은 공개 모델의 import 확인 예다. 이 모델의 성공을 Gemma 학습 검증과 동일시하지 않는다. 각 POST를 한 번씩 요청하고 반환된 두 ID를 보관한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  API_BASE='http://127.0.0.1:13000'

  MODEL_RESPONSE=$(curl -fsS -X POST "$API_BASE/api/models" \
    -H 'Content-Type: application/json' \
    -d '{"name":"manual-import-check","model_type":"llm","description":"manual import check"}')
  MODEL_ID=$(printf '%s' "$MODEL_RESPONSE" | jq -er '.model_id')

  IMPORT_RESPONSE=$(curl -fsS \
    -X POST "$API_BASE/api/models/$MODEL_ID/imports/huggingface" \
    -H 'Content-Type: application/json' \
    -d '{"version":"v1","repo_id":"hf-internal-testing/tiny-random-gpt2","revision":"main"}')
  printf '%s\n' "$IMPORT_RESPONSE" | jq .
)
```

`repo_id`가 gated/private이면 접근 권한이 있는 HF token을 요청의 `hf_token`에 함께 넣는다. token을 소스나 셸 이력에 직접 적지 않으려면 Bash의 `read -rs`로 입력받고 `jq --rawfile`의 process substitution을 이용할 수 있다. 기존 MODEL_ID와 접근할 repository를 지정한 뒤 아래처럼 요청한다.

```bash
(
  set -euo pipefail
  API_BASE='http://127.0.0.1:13000'
  MODEL_ID='<생성한 model UUID>'
  read -r -s -p 'Hugging Face token: ' IMPORT_HF_TOKEN
  printf '\n'
  trap 'unset IMPORT_HF_TOKEN' EXIT

  jq -n --rawfile token <(printf '%s' "$IMPORT_HF_TOKEN") '{
    version: "v1", repo_id: "google/gemma-3-1b-it", revision: "main", hf_token: $token
  }' | curl -fsS \
    -X POST "$API_BASE/api/models/$MODEL_ID/imports/huggingface" \
    -H 'Content-Type: application/json' --data-binary @- | jq .
)
```

새 모델에는 `v1`을 쓰고 이미 같은 버전이 있는 모델에는 다른 version을 지정한다. Operation이 succeeded, model version이 ready/snapshot이고 artifact_uri가 생기는지 확인한다. import한 실제 snapshot ID는 다음 학습 입력으로 사용할 수 있다.

## Gemma 단일 GPU QLoRA 1-step 요청

아래 기본 ID는 개발 서버에서 검증된 Gemma snapshot이다. 새 DB에서는 위 import 결과의 ID로 바꾼다. 요청 전에 ready/snapshot을 검사한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  API_BASE='http://127.0.0.1:13000'
  MODEL_VERSION_ID='cceae526-7b7c-47d4-870b-117372b51885'

  curl -fsS "$API_BASE/api/model-versions/$MODEL_VERSION_ID" |
    jq -e '.state == "ready" and .artifact_kind == "snapshot"' >/dev/null

  TRAINING_RESPONSE=$(
    jq -n --arg id "$MODEL_VERSION_ID" '{
      source_model_version_id: $id,
      dataset: {source_type: "synthetic", format: "chat_messages"},
      training: {
        method: "qlora", sequence_length: 256, max_steps: 1,
        micro_batch_size: 1, gradient_accumulation_steps: 1,
        learning_rate: 0.0002,
        lora: {r: 8, alpha: 16, dropout: 0.0}, sample_packing: false
      },
      resources: {gpu_ids: [0]}
    }' | curl -fsS -X POST "$API_BASE/api/training-runs" \
      -H 'Content-Type: application/json' --data-binary @-
  )
  printf '%s\n' "$TRAINING_RESPONSE" | jq .
  printf 'TRAINING_RUN_ID=%s\n' "$(printf '%s' "$TRAINING_RESPONSE" | jq -er '.training_run_id')"
  printf 'OPERATION_ID=%s\n' "$(printf '%s' "$TRAINING_RESPONSE" | jq -er '.operation_id')"
)
```

위 변수는 서브셸 밖에 남지 않는다. 출력한 ID를 다음 블록에 넣는다. 단일 GPU 사용 가능 여부는 테스트 전에 확인한다.

```bash
API_BASE='http://127.0.0.1:13000'
OPERATION_ID='<출력한 operation UUID>'
watch -n 2 "curl -fsS '$API_BASE/api/operations/$OPERATION_ID' | jq '{status,phase,attempt,error_code,error_message}'"
```

succeeded 또는 failed를 확인하면 Ctrl-C로 조회를 끝낸다. failed면 Operation 오류와 해당 Job 로그를 먼저 확인한다.

## 결과와 MLflow 확인

Operation 성공 후 출력한 training ID를 사용한다.

```bash
(
  set -euo pipefail
  API_BASE='http://127.0.0.1:13000'
  TRAINING_RUN_ID='<출력한 training UUID>'
  RUN=$(curl -fsS "$API_BASE/api/training-runs/$TRAINING_RUN_ID")
  printf '%s\n' "$RUN" | jq '{
    training_run_id,status,mlflow_experiment_id,mlflow_run_id,mlflow_run_url,
    output_model_version_id,result
  }'
  printf '%s' "$RUN" | jq -e '.status == "succeeded" and .result.train_exit_code == 0' >/dev/null
  OUTPUT_ID=$(printf '%s' "$RUN" | jq -er '.output_model_version_id | select(length > 0)')
  OUTPUT=$(curl -fsS "$API_BASE/api/model-versions/$OUTPUT_ID")
  printf '%s\n' "$OUTPUT" | jq '{
    model_version_id,state,artifact_kind,training_run_id,source_model_version_id,
    artifact_uri,artifact_size_bytes,artifact_file_count,artifact_manifest_sha256
  }'
  printf '%s' "$OUTPUT" | jq -e '.state == "ready" and .artifact_kind == "adapter"' >/dev/null
)
```

MLflow run URL에서 해당 Training Run의 params·metrics를 확인한다. 기본 experiment는 `model-platform-training`이다. 출력 크기·파일 수는 실행 설정에 따라 달라지므로 과거 20개/약 2.55GB와 일치해야 한다는 검증 조건을 두지 않는다. Artifact·manifest 의미와 부분 실패 처리 범위는 [학습 계약](../reference/training.md)에 있다. 생성한 학습·모델 결과는 진단과 이후 사용을 위해 남는다.
