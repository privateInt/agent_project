# 학습·평가 데이터 분리와 결과 비교

TrainingRun에 학습용 버전과 별도 평가용 버전을 고정하고, 학습을 끝낸 모델의 마지막 답변 토큰 loss를 기록한다. 평가를 요청한 실행은 평가·결과물 검증까지 성공해야 succeeded가 된다. 서버 적용·GPU 확인은 [검증 절차](../testing/training-evaluation.md)를 따른다.

## 요청과 입력 조건

기존 `POST /api/training-runs`에 선택적 `evaluation`을 추가했다.

```json
{
  "source_model_version_id": "<ready snapshot UUID>",
  "dataset": {"source_type": "version", "dataset_version_id": "<train version UUID>"},
  "evaluation": {
    "dataset": {"source_type": "version", "dataset_version_id": "<eval version UUID>"}
  },
  "training": {
    "method": "qlora", "sequence_length": 256, "max_steps": 2,
    "micro_batch_size": 1, "gradient_accumulation_steps": 1,
    "learning_rate": 0.0002, "lora": {"r": 8, "alpha": 16, "dropout": 0},
    "sample_packing": false
  },
  "resources": {"gpu_ids": [0]}
}
```

학습용·평가용 JSONL을 각각 등록한다. 같은 Dataset의 서로 다른 버전 또는 다른 Dataset의 버전을 사용할 수 있다. 비율을 받아 자동 분할하는 기능은 없다. 기존 요청에서 evaluation 생략 또는 null은 평가를 사용하지 않으며 이전 생성 요청 fingerprint를 유지한다.

| 항목 | 규칙 |
|---|---|
| 실행 | 로컬 단일 GPU QLoRA, sample_packing=false, sequence_length 2..4096 |
| 데이터 | 양쪽 모두 source_type=version. URI·SHA-256·크기·전체 레코드 수는 서버가 고정 |
| 파일 | 기존 등록 한도인 파일당 64 MiB. 학습 최소 2개, 평가 2..2048개 레코드 |
| 대화 | messages만 있는 JSONL, 레코드당 2..256개 메시지. 선택적 처음 system 뒤 user/assistant 교대, 마지막 assistant 필수 |
| 정답 | 마지막 assistant.content에 비어 있지 않은 참조 답변 필요 |
| 분리 | 같은 버전 ID 또는 같은 파일 SHA-256은 API에서 거부. 모든 학습/평가 대화의 assistant 직전 입력 중복은 Job에서 거부 |
| 중복 검사 | 전체 대화 접두부를 NFC 정규화·앞뒤 공백 제거 후 비교하고, 실제 tokenizer로 토큰화한 접두부도 비교. 평가의 마지막 입력 중복도 거부 |
| 길이 | 평가 전체 대화가 sequence_length를 넘으면 중단. 평가 행을 자르거나 일부만 건너뛰어 점수를 내지 않음 |

중복 검사는 현재 등록된 학습 파일과 평가 파일 사이의 입력을 대조한다. 의미가 비슷한 질문이나 원본 모델 사전학습 데이터까지 판별하는 기능은 아니다. 일반 등록에서 허용하던 데이터라도 이 평가 경로의 대화 조건을 충족하지 못하면 실행 준비가 실패할 수 있다.

API는 evaluation 하위에 dataset만 받고, dataset에는 source_type·format·dataset_version_id만 허용한다. 새 평가 필드의 대소문자 오류·알 수 없는 필드·중복 JSON 키를 거부한다. 클라이언트가 평가 pin을 덮어쓸 수 없다.

## 평가 지표

프로토콜 이름은 `final_assistant_nll_v1`이다. Trainer.train이 요청한 마지막 step을 완료한 뒤 같은 모델을 eval 모드로 전환해 추가 가중치 업데이트 없이 평가한다. 평가 데이터는 Axolotl의 학습 dataset 목록에 넣지 않는다. 평가 후 원래 모델 모드를 복구하고 기존 최종 저장·업로드 경로로 진행한다.

학습 tokenizer의 chat template로 마지막 assistant 전까지의 생성 프롬프트와 정답까지 포함한 전체 대화를 각각 토큰화한다. 생성 프롬프트가 전체 대화 토큰의 정확한 접두부일 때만 정답 구간을 정한다. **마지막 assistant 답변과 template의 닫는 토큰을 채점하고, 앞선 대화는 조건으로만 사용한다.** tokenizer가 이미 넣은 특수 토큰을 다시 추가하지 않는다. [Transformers chat template 설명](https://huggingface.co/docs/transformers/en/chat_templating)

샘플당 배치 1, CUDA BF16 forward와 FP32 cross-entropy 합계를 사용한다. 위치 t-1의 출력으로 정답 토큰 t를 채점한다.

- `nll_sum`: 채점한 모든 정답 토큰의 음의 로그 확률 합계.
- `scored_tokens`: 채점한 정답 토큰 수. template 닫는 토큰도 포함.
- `loss = nll_sum / scored_tokens`: 토큰 수로 가중한 평균. 레코드별 평균을 다시 평균하지 않는다.
- `perplexity = exp(loss)`: loss가 700을 넘으면 JSON null과 perplexity_overflow=true를 사용한다.

Perplexity는 평균 음의 로그 확률의 지수이며 토큰화가 값에 영향을 준다. [Transformers perplexity 설명](https://huggingface.co/docs/transformers/en/perplexity) 여기서는 전체 말뭉치 perplexity 대신 위에 정의한 **참조 답변 구간의 조건부 perplexity**를 계산한다. 같은 조건에서 낮을수록 참조 답변에 더 높은 확률을 부여했다는 뜻이다. 생성 답변의 정답률·사용자 만족도·일반적인 모델 품질을 의미하지 않는다. 학습 전 base 점수, 학습 도중 validation, best checkpoint 선택, 생성형 평가·자동 채점은 포함하지 않는다.

## 결과 조회와 비교

| REST | 동작 |
|---|---|
| GET /api/training-runs/{id} | 기존 상세에 evaluation.dataset pin 추가 |
| GET /api/training-runs | 각 요약에 evaluation 추가. 기본 목록·필터·커서 유지 |
| GET /api/training-runs/{id}/evaluation | 평가 상태·pin·지표·출력 모델·MLflow 연결 조회. body/query 없음 |
| GET /api/training-runs/compare?training_run_ids=id1,id2 | 서로 다른 2..5개 TrainingRun을 요청 순서로 조회. 다른 query/body 없음 |

평가 상태는 disabled(요청하지 않음), pending(진행 중), unavailable(실패/취소되어 완료 평가 없음), ready(검증된 완료 평가)다. ready가 되려면 원래 완료 결과 또는 성공한 업로드 전용 복구 결과가 있어야 하며, 해당 출력 모델의 ready/adapter·TrainingRun·base·manifest도 일치해야 한다. 증거 손상은 disabled나 성공으로 바꾸지 않고 오류를 반환한다.

result에는 protocol·compatibility_sha256·tokenized_sha256·dataset_version_id·dataset_sha256·record_count·scored_tokens·sequence_length·trained_steps·nll_sum·loss·perplexity·perplexity_overflow·source_model_version_id·training_run_id가 있다. 호스트 경로·실행 승인 토큰·optimizer 내용은 평가 응답에 넣지 않는다.

비교 응답은 `{"comparable":true,"reason":"","items":[...]}` 형태다. 각 item에 `loss_delta_from_first`를 추가한다. 다음 조건이 전부 같아야 comparable=true다.

1. 모든 결과가 ready이고 평가 데이터 **버전 ID와 SHA-256**이 같다.
2. 원본 모델 버전, 실제 내려받은 base 파일 digest가 같다.
3. protocol·평가 코드·Axolotl/Transformers/Torch/PEFT/bitsandbytes/Accelerate 버전·정밀도 규칙이 같다.
4. 실제 토큰 ID와 채점 시작 위치, sequence_length·전체 레코드 수·채점 토큰 수가 같다.

학습 step·학습 데이터·LoRA 설정은 달라도 비교할 수 있다. 같은 평가 내용이라도 다른 버전 ID면 다른 평가 데이터로 처리한다. 조건이 다르면 행은 반환하되 comparable=false, loss_delta_from_first=null이다. reason은 evaluation_not_ready, different_evaluation_dataset, different_base_model, different_evaluation_protocol 중 하나다. 비교 GET은 새 학습·서빙 Job을 만들지 않는다.

## Checkpoint·출력 복구·MLflow 연결

Checkpoint receipt에 평가 pin을 함께 저장하고 재개 요청은 부모의 evaluation_spec을 그대로 복사한다. Job도 부모·자식·receipt의 pin을 대조한다. 기존 config_adapter.py·checkpoint_store.py·checkpoint_train.py의 바이트를 바꾸지 않아 이전 평가 미사용 checkpoint의 코드 signature는 유지한다. 새 evaluation wrapper가 기존 checkpoint wrapper를 감싸며, 새 실행이 목표 step까지 완료된 뒤 평가한다.

평가 결과는 완료 출력 receipt와 result_json에 포함된다. Job, Training 완료 commit, 업로드 전용 복구가 같은 지표·pin 검증을 적용한다. 업로드 복구로 얻은 결과는 result_origin=output_recovery로 표시하고 원래 failed 이력을 유지한다. 검증 후 로컬 출력 release를 해도 DB의 평가 결과는 유지된다. 학습/평가가 끝나지 않아 완료 receipt가 없는 경우 업로드 전용 복구로 성공시킬 수 없다.

MLflow가 활성화된 경우 기존 run ID에 heldout_loss·heldout_perplexity·heldout_scored_tokens·heldout_record_count를 최종 step으로 기록한다. perplexity overflow이면 해당 지표를 생략한다. protocol·dataset_version_id·compatibility_sha256도 tag로 남긴다. 이 경로는 연결·기록 실패를 무시하지 않는다. [MlflowClient.log_batch](https://mlflow.org/docs/latest/api_reference/python_api/mlflow.client.html#mlflow.client.MlflowClient.log_batch)

Training 기동 시 evaluation_spec JSONB 열을 추가한다. 학습 Job에는 model-platform.training-evaluation=1 라벨이 필요하며 Dispatcher가 생성 전에 검사한다. 실행 이미지 교체 대상은 학습 Job과 Training·Gateway·Dispatcher다. 새 상시 서비스·Kubernetes 구성은 추가하지 않았다.
