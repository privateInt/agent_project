# 학습 계약과 런타임

현재 DatasetSpec/Training Job 계약을 설명한다. 2026-10-12 [확장 F1/F5 계획](../plans/platform-expansion-work-plan-20261012.md)은 Data 학습데이터 소유/불변 입력 resolve와 instruction/RAFT adapter의 단계 도입이며 아직 구현하지 않았다. 학습 실행·checkpoint·평가 실행·모델 결과는 Model에 유지하고 기존 source_type=version·요청 fingerprint·과거 입력 pin을 보존한다.

## 요청

`POST /api/training-runs`는 플랫폼의 DatasetSpec·TrainingSpec을 받는다. Job이 이를 Axolotl YAML로 변환한다. 호출자가 Axolotl 내부 YAML을 직접 전달하지 않는다.

```json
{
  "source_model_version_id": "<ready snapshot UUID>",
  "dataset": {"source_type": "synthetic", "format": "chat_messages"},
  "training": {
    "method": "qlora",
    "sequence_length": 256,
    "max_steps": 1,
    "micro_batch_size": 1,
    "gradient_accumulation_steps": 1,
    "learning_rate": 0.0002,
    "lora": {"r": 8, "alpha": 16, "dropout": 0.0},
    "sample_packing": false
  },
  "resources": {"gpu_ids": [0]}
}
```

응답의 `training_run_id`는 도메인 조회, `operation_id`는 실행 상태 조회에 사용한다. 현재 검증 경로의 source version은 `ready/snapshot`이다. API는 ready를 확인하고 adapter 입력을 명시적으로 거부한다. Legacy file이 API에서 수락되어도 학습 가능한 모델이라는 보장은 없다. Adapter 이어 학습은 아직 지원하지 않는다. 학습 결과의 추론 배포는 [adapter 서빙 계약](adapter-serving.md)을 따른다.

## DatasetSpec

| 필드 | 허용 값 / 규칙 |
|---|---|
| source_type | `synthetic`(기본), `s3`, `version` |
| source_uri | synthetic이면 비어 있어야 함. s3이면 단일 `s3://bucket/path/train.jsonl`. version이면 서버가 채움 |
| format | `chat_messages`만 지원 |
| dataset_version_id | source_type=version일 때 등록된 버전 UUID 필수 |
| sha256, size_bytes, record_count | version의 서버 소유 pin. 요청에 지정하면 거부하며 TrainingRun 조회·결과에서 제공 |

`synthetic`은 결정적인 2개 레코드로 구성한 통합 테스트 fixture다. 실제 파일을 고정해 학습하려면 [데이터셋 등록·버전 계약](datasets.md)의 `source_type=version`을 사용한다. 기존 `s3` 직접 지정도 유지하지만 내용 고정은 보장하지 않는다. 학습용·평가용 버전을 별도로 고정하는 선택적 [evaluation 요청과 결과 비교](training-evaluation.md)를 추가했다. 여러 파일, parquet, 비율에 따른 자동 분할은 지원하지 않는다.

JSONL의 한 줄 예:

```json
{"messages":[{"role":"user","content":"2+3?"},{"role":"assistant","content":"5"}]}
```

## TrainingSpec과 Axolotl 변환

| API 필드 | 기본값 | Axolotl 설정 |
|---|---|---|
| method | `qlora` | `adapter: qlora`, `load_in_4bit: true` |
| sequence_length | 256 | sequence_len |
| max_steps | 1 | max_steps |
| micro_batch_size | 1 | micro_batch_size |
| gradient_accumulation_steps | 1 | gradient_accumulation_steps |
| learning_rate | 0.0002 | learning_rate |
| lora.r | 8 | lora_r |
| lora.alpha | 16 | lora_alpha |
| lora.dropout | 0.0 | lora_dropout |
| sample_packing | false | sample_packing |
| checkpoint_steps | 0 (사용 안 함) | 양수는 save_steps. 로컬 단일 GPU·고정 데이터·unpacked 학습에서 [checkpoint 보존·재개](training-checkpoints.md) 사용 |
| strategy | GPU 1개는 single, 2개 이상은 ddp | [로컬 DDP 실행](multi-gpu.md). 단일 GPU의 정규화 저장 값은 빈 문자열 |

숫자 0은 일부 필드에서 생략한 값으로 처리해 기본값을 적용한다. seq는 1..131072, step·batch·accumulation·rank·alpha는 양수, LR은 유한 양수, dropout은 유한 0 이상 1 미만이다. 정확한 정규화·검증은 [spec.go](../../src/platform/model/training/app/spec.go), 변환은 [config_adapter.py](../../src/platform/model/jobs/training/axolotl/config_adapter.py)를 기준으로 한다. 요청은 정규화 후 DB에 저장한다.

## Resources

Training·Deployment는 공통 `node_name`, `gpu_count`, `gpu_ids`를 받는다. GPU ID는 음이 아닌 서로 다른 값이어야 하며, count를 함께 지정하면 ID 개수와 일치해야 한다. Training은 기본 GPU 1개이며 같은 호스트의 정확한 IDs를 지정하면 최대 16개의 DDP 요청을 받는다. 다중 GPU에서 count만 지정한 요청은 거절한다. Deployment demo는 기본 0개다.

`node_name`을 지정하면 `MODEL_DISPATCHER_NODE_NAME`과 일치해야 한다. 현재 Docker 실행기는 로컬 노드에서만 동작한다. `gpu_ids`는 Docker의 `--gpus device=...`로 변환한다. 호스트 GPU 번호와 컨테이너 내부 `cuda:0` 번호는 다를 수 있다. Dispatcher·Serving은 [공통 GPU 점유 관리](multi-gpu.md)를 사용한다. 새 구현의 실제 서버 검증 상태는 [현재 상태](../status.md)를 따른다.

## 런타임과 추적

학습 취소와 현재/보관 로그 조회는 [학습 제어 계약](training-control.md)을 따른다. 로컬 취소는 `cancelling`을 거쳐 실제 중지 확인 후 `cancelled`로 전환하며, 완료 결과를 덮어쓰지 않는다.

[학습 Dockerfile](../../src/platform/model/jobs/training/axolotl/Dockerfile)의 training-job은 Axolotl 0.18.0 이미지, Go lifecycle binary, config adapter, MLflow client를 묶는다. 공통 Go 바이너리는 [Bake 빌드 연결](../operations/build.md)로 현재 소스에서 받는다. 상시 학습 서비스가 아니라 요청별 one-shot Job이다.

Job은 승인 후 명세와 base snapshot을 조회하고 MinIO에서 임시 작업 디렉터리로 내려받는다. HF offline 설정으로 preprocess와 train을 수행한다. 런타임의 base model을 Hugging Face에서 다시 받는 흐름은 아니다.

학습 전에 MLflow experiment/run 연결을 Training DB에 저장한다. 기본 experiment는 `model-platform-training`, run name은 training_run_id다. Axolotl 학습 로그는 컨테이너 로그로 출력되고 metrics/params는 공통 MLflow에 기록한다. `hf_mlflow_log_artifacts=false`이므로 모델 파일을 MLflow에 중복 업로드하지 않는다. 최종 adapter는 플랫폼 MinIO에 보관한다. checkpoint_steps를 켠 실행의 재개용 checkpoint는 별도 로컬 영속 볼륨에 두고, 작업용 checkpoint 디렉터리를 최종 adapter 업로드에서 제외한다.

`GET /api/training-runs/:id`의 `result_json`은 원본 JSON **문자열**, `result`는 Gateway가 파싱한 객체다. 조회 시 `.result`를 사용하면 이중 파싱이 필요 없다. `mlflow_*`와 `output_model_version_id`는 최상위 필드로도 제공한다.

## 결과물 등록 순서

1. DDP이면 모든 worker의 실제 참여·완료 step·고유 장치 증거를 검증한다. 학습 출력 디렉터리에서 파일별 SHA256·크기를 담은 canonical manifest를 만든다. 최상위 `adapter_config.json`, `adapter_model.safetensors`가 필요하다.
2. Management가 training_run_id당 하나의 output version을 예약한다. 같은 manifest 재요청은 허용하고 다른 manifest는 거부한다.
3. `models/{model_id}/versions/{output_version_id}/outputs/{manifest_sha256}/` 아래에 파일을 업로드한다.
4. MinIO에서 실제 바이트를 다시 읽어 검증하고 manifest를 마지막에 쓴다. Management도 원격 검증을 반복한 뒤 `ready/adapter`로 확정한다.
5. Training의 완료 결과와 output version ID를 저장하고 Operation에 성공을 보고한다. 등록 데이터셋을 사용했다면 완료 결과의 버전·URI·hash·크기·레코드 수가 TrainingRun의 pin과 일치해야 한다. DDP는 `.result.parallelism`의 모든 rank가 저장된 GPU 수·step·batch와 일치해야 한다.

`artifact_sha256`과 `artifact_manifest_sha256`은 adapter의 경우 manifest digest다. 각 파일 digest는 manifest 안에 있다. `artifact_size_bytes`와 `artifact_file_count`에는 manifest 자체를 포함하지 않는다. Version은 원본 모델에 속하며 training_run_id, source_model_version_id, base_model_artifact_uri를 보존한다.

미완료 업로드는 ready가 되지 않는다. 이미 ready인 결과물은 후속 Training 완료 오류가 나도 자동 삭제하지 않는다. 부분 결과 참조를 보존하지만, Training/Management의 미완료 메타데이터 전체를 Reconciler가 자동 복구한다는 뜻은 아니다. 중간 객체 GC와 checkpoint 원격 동기화는 후속 작업이다. 로컬 checkpoint 보존·재개는 [별도 계약](training-checkpoints.md)을 따른다.


새 로컬 Job은 완료 출력을 영속 볼륨에 보존한다. 학습 이후 업로드·등록이 실패하면 [출력 복구 API](training-output.md)로 같은 파일의 업로드만 재시도할 수 있다. 원래 실패 상태를 바꾸지 않으며, 성공한 복구는 `output_recovery_status=succeeded`와 `recovered_model_version_id`로 확인한다.
