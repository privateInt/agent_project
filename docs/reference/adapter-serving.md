# 학습 adapter의 vLLM 서빙 계약

[기존 서빙 수명주기](serving.md)에 학습 adapter와 원본 base snapshot을 함께 로드하는 경로를 추가한다. 구현·검증 상태는 [현재 상태](../status.md), 적용 순서는 [adapter 테스트](../testing/adapter-serving.md)가 기준이다.

## 입력과 원본 연결

기존 POST에 학습 결과의 모델 버전 ID를 넣는다. base를 사용자가 별도로 선택하지 않는다.

```json
{
  "model_version_id": "80e34e1f-f5da-4448-a0ca-282e76d78526",
  "resources": {"gpu_ids": [0]},
  "serving": {
    "engine": "vllm",
    "max_model_len": 2048,
    "gpu_memory_utilization": 0.5
  }
}
```

요청 수락 시 Management에서 아래 연결을 검증하고 URI·manifest SHA-256·크기를 Deployment의 `serving_source`에 고정한다. 이후 controller는 고정된 값을 사용한다.

| 대상 | 필수 조건 |
|---|---|
| adapter | ready, source_type=training, artifact_kind=adapter, 유효한 training_run_id |
| adapter manifest | artifact_sha256=artifact_manifest_sha256, 양수 크기·파일 수 |
| base | adapter.source_model_version_id로 정확히 조회한 ready/huggingface/snapshot, LLM 모델 |
| 연결 | adapter와 base의 model_id 일치, base_model_artifact_uri와 base.artifact_uri 일치 |

`adapter_config.json`의 일시적인 학습 디렉터리나 repository의 최신 revision으로 base를 찾지 않는다. 기존 예제 adapter의 base는 `cceae526-7b7c-47d4-870b-117372b51885`다. 같은 Idempotency-Key 재전송은 처음 수락한 ID를 재현하며 Management가 일시적으로 조회되지 않아도 새 배포를 만들지 않는다.

## 결과물 검증과 서빙용 사본

base는 기존 `_manifest.json`과 모든 파일을 검증한다. adapter는 전체 `artifact-manifest.json`의 canonical JSON·digest·전체 크기·파일 수를 등록값과 대조하고, 아래 root 파일만 다운로드하여 개별 SHA-256·크기를 검사한다.

- 필수: `adapter_config.json`, `adapter_model.safetensors`.
- manifest에 있으면: `tokenizer.json`, `tokenizer_config.json`, `chat_template.jinja`. format 2는 `special_tokens_map.json`, `added_tokens.json`도 있으면 포함한다.
- 중간 checkpoint, optimizer 상태, 학습 로그는 서빙에 다운로드하지 않는다. 이 단계는 사용하지 않는 원격 파일까지 재검사하는 전체 아카이브 검증이 아니다.

현재 새 요청은 `serving_format=2`를 고정한다. 이미 저장된 format 1 요청은 원래 준비 방식을 유지한다.

1. safetensors의 길이·dtype·shape·offset·중복 이름을 검사한다. 지원 dtype은 BF16/F16/F32다.
2. 원본 base의 가중치로 별도 `adapter-base/`를 만든다. Gemma3ForCausalLM의 full embed_tokens·lm_head가 모두 저장된 경우 두 행렬을 그대로 넣는다. hidden size·양쪽 shape·base config를 검사하고, 행 수 축소는 거절한다.
3. 이 경우 vocab_size를 저장 행 수에 맞추고 tie_word_embeddings=false로 설정해 두 행렬을 독립적으로 로드한다. 원본과 같은 행인지, 두 행렬끼리 같은지는 가정하지 않는다. 다른 모델이나 full matrix가 없는 결과물은 기존 embedding 동일성·tokenizer 검사를 통과해야 한다.
4. 실행용 base와 LoRA A/B 쌍의 rank·차원을 확인한다. 저장된 full matrix가 실행용 base에 포함되었는지 실제 값 비교 후 LoRA 파일에서는 제외한다.
5. 원본 LoRA tensor 바이트를 그대로 복사해 `adapter/`에 둔다. merge·재학습·embedding 초기화·행 삭제·dtype 변환 없이 원본 MinIO 객체와 다운로드한 원본을 보존한다.

Gemma3ForCausalLM의 vocab_size와 tie_word_embeddings에 따른 embedding·출력층 로딩은 [vLLM 0.15.0 구현](https://docs.vllm.ai/en/v0.15.0/api/vllm/model_executor/models/gemma3/)을 기준으로 한다. 실제 보유 가중치의 로드·추론은 GPU smoke로 별도 확인한다. QLoRA 학습 시의 4-bit 양자화 연산 자체를 재현하는 방식은 아니다.

## tokenizer와 채팅 템플릿

full embedding을 복원하는 Gemma 경로에서는 저장된 tokenizer.json과 tokenizer_config.json을 필수로 사용한다. tokenizer의 model·normalizer·pre/post processor·decoder·padding·truncation은 base와 같아야 한다. added token은 ID별로 비교하며 중복·범위 밖 ID를 거절한다. 토큰 ID·내용·encoding 옵션은 같아야 하고 `special` 변경만 허용한다. 이 변경을 무시하는 것이 아니라 학습 tokenizer를 사용해 보존한다. BOS/EOS/PAD/UNK의 토큰 내용은 원본과 같아야 한다. 자동 삽입 옵션 add_bos_token/add_eos_token은 학습 때 저장된 boolean 값을 그대로 사용한다. 생략 여부도 보존하며 원본 값과의 동일성을 강제하거나 기본값을 채우지 않는다. null·문자열·숫자는 거절한다. 다른 어휘 변경이나 새 모델 계열의 resize 지원은 별도 작업이다. 두 자동 삽입 옵션의 역할은 [Transformers Gemma tokenizer 문서](https://huggingface.co/docs/transformers/en/model_doc/gemma#gemmatokenizerfast)에 설명되어 있다.

어휘 ID 범위는 저장 embedding 행 수와 대조한다. 모델의 어휘 행 수보다 큰 ID를 tokenize할 수 있는 상태를 허용하지 않는다. 저장 fast tokenizer를 선택할 때는 원본 SentencePiece/vocab sidecar를 섞지 않는다. special_tokens_map.json과 added_tokens.json이 학습 manifest에 있으면 함께 검증·사용한다.

채팅 템플릿은 adapter의 chat_template.jinja → adapter tokenizer_config의 단일 문자열 → base의 같은 두 위치 순으로 선택한다. 비어 있거나 여러 템플릿인 값은 임의 대체하지 않는다. 선택한 템플릿을 `adapter/`에 보존하고 `--chat-template`으로 명시한다. [vLLM 채팅 템플릿](https://docs.vllm.ai/en/v0.15.0/serving/openai_compatible_server/#chat-template)

## 캐시와 증거

| 경로 (`/serving-cache/<deployment_id>/` 기준) | 내용 |
|---|---|
| model/ | 검증한 원본 base snapshot, 수정하지 않음 |
| adapter-source/ | 검증한 원본 adapter 파일과 전체 manifest |
| adapter-base/ | format 2의 실행용 모델, 선택한 tokenizer·config·serving-base.json |
| adapter/ | LoRA 가중치·adapter_config·채팅 템플릿·serving-adapter.json |

`serving-base.json`에는 Plan hash, 원본 base/adapter digest, 보존한 embedding 이름, 복원한 vocab_size, tokenizer 출처, 준비 파일의 SHA-256·크기를 기록한다. 기존 shard index는 복사하지 않고 모든 tensor가 한 model.safetensors를 가리키도록 새 header를 쓴다. 재사용 시 원본에서 다시 계산한 내용과 대조해 손상·예상 밖 파일·symlink를 거절한다. 미완료 사본은 공개하지 않고 기존 사본을 덮어쓰지 않는다.

`serving-adapter.json`에는 format, 원본 digest, rank, LoRA 수·hash·크기, 템플릿 출처·hash를 기록한다. `verified_duplicate_embeddings`는 **format 1이면 원본 base, format 2이면 adapter-base와 같은 값임을 확인한 이름**이다. format 2에서 원본 base와 같다는 뜻은 아니다. 실제 보존 위치와 행 수는 serving-base.json을 함께 확인한다.

준비 모델은 원본과 별도 디스크 공간을 쓴다. 이번 1B BF16 모델은 추가 출력층 때문에 가중치 사본만 대략 2.6 GB가 필요하며, 재검증 중에는 임시 사본 공간도 필요하다. 독립 lm_head는 tied 상태보다 GPU 메모리를 더 사용한다. 자동 캐시 정리는 아직 없다.

### format 1 호환성

기존 format 1 배포는 원래 `model/`과 base tokenizer를 사용한다. 저장 embedding이 원본 base와 shape·값이 같을 때만 LoRA 파일에서 제외하며 tokenizer 변경은 거절한다. 그 요청을 새 규칙으로 재해석하지 않는다. 변경된 tokenizer/embedding을 가진 이미 stopped인 배포에는 새 키로 새 배포를 만든다.

## 런타임과 모델 이름

기존 `vllm/vllm-openai:v0.15.0`에 static LoRA 하나를 등록한다. `--enable-lora`, `--max-loras 1`, `--max-lora-rank 64`와 JSON 형식 `--lora-modules`를 사용한다. rank 1..64의 causal LoRA가 현재 범위이며 DoRA, bias 학습, modules_to_save, per-module rank/alpha pattern, target_parameters는 거절한다. rsLoRA 설정은 보존한다. 실행 중 adapter 교체 API는 활성화하지 않는다. [vLLM LoRA 사용법](https://docs.vllm.ai/en/v0.15.0/features/lora/), [PEFT 옵션 검사](https://docs.vllm.ai/en/v0.15.0/api/vllm/lora/peft_helper/)

| Deployment 조회 필드 | adapter 배포의 값 |
|---|---|
| model_version_id / adapter_model_version_id | 요청한 adapter 버전 ID |
| artifact_kind | adapter |
| base_model_version_id | 고정한 원본 snapshot ID |
| served_model_name | `deployment-<deployment_id>`: adapter 추론에 사용 |
| base_served_model_name | `deployment-<deployment_id>-base`: 실행용 base 이름. format 2에서는 저장 embedding이 반영된 모델 |

readiness는 `/health`와 `/v1/models`를 확인한다. base 이름만 존재해서는 성공하지 않으며 adapter의 id·parent·root가 고정한 이름·경로와 모두 맞아야 한다. GPU smoke는 adapter 이름으로 채팅을 요청하고 응답의 model 값도 검사한다. base와 답변이 다르다는 조건은 사용하지 않는다. 1 step 학습이 답변 변화나 품질 향상을 보장하지 않기 때문이다.

vLLM 0.15.0의 지원 목록에는 Gemma3ForCausalLM의 LoRA 지원이 표시되어 있다. 실제 보유 adapter의 가중치·GPU 실행 호환성은 별도 smoke 결과로 판단한다. [지원 모델](https://docs.vllm.ai/en/v0.15.0/models/supported_models/)

## 배포 호환성과 한계

공개 API engine은 계속 `vllm`이다. 내부 DB에서는 adapter 요청을 `vllm-lora`로 저장하여 구버전 snapshot controller가 이를 base-only 요청으로 실행하지 못하게 한다. source와 내부 engine이 어긋난 행도 거절한다. snapshot과 format 1의 기존 Plan JSON·컨테이너 소유권 해시 및 생성 요청 fingerprint는 유지한다. format 2는 별도 모델 경로를 고정한다. 알려지지 않은 format은 실행하지 않는다. format 2를 지원하지 않는 구버전은 해당 배포를 관리하지 못하므로 rollback 전에 현 버전으로 Stop을 완료한다.

최초 adapter 적용은 Deployment를 먼저, Gateway를 나중에 교체한다. format 2 보완은 Deployment만 교체한다. adapter 배포가 존재하는 동안에는 새 Deployment를 유지해야 조회·중지·관찰이 가능하다. 구버전으로 rollback하면 adapter 관리가 불가능하므로 먼저 새 버전에서 해당 배포를 Stop하고 상태를 확인한다. 스키마 추가는 되돌리거나 데이터를 삭제할 필요가 없다.

시작 Job은 기존 `deployment.serve`/`serving-v1`를 재사용한다. Operation, Dispatcher, Reconciler, GC 계약은 그대로다. 후속 [로컬 GPU 점유 관리](multi-gpu.md)가 Training과의 충돌을 검사한다. 서빙 다중 GPU·자동 재시작·자동 캐시 GC·범용 추론 proxy는 없다. 텍스트 채팅은 Gateway의 배포별 chat API를 지원한다. base는 기존 snapshot dtype으로 로드하며 학습 때 사용한 QLoRA 4-bit 실행을 재현하는 옵션은 아니다.
