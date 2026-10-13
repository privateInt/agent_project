# Model 임베딩 API — K03

상태: 2026-09-18 소스 구현 후 기존 runtime의 공식 API·서버 Go/이미지·Python/UI 검증을 확인했다. 실제 범위는 [적용·검증](../testing/embedding.md)과 [현재 상태](../status.md)를 따른다. 신규 profile GPU 배포·범용 모델 지원까지 완료한 것은 아니다.

## 책임과 지원 범위

Model Deployment가 배포·artifact·tokenizer를 확인하고 전처리·토큰 검사·추론을 수행한다. Gateway는 REST를 같은 Model 내부 gRPC로 전달한다. Knowledge는 이 계약의 소비자이며 모델이나 tokenizer를 로드하지 않는다. LiteLLM은 현재 경로에 없다.

첫 지원 profile은 `snowflake-ko-v1`, 저장소는 `dragonkue/snowflake-arctic-embed-l-v2.0-ko`이다. snapshot만 지원한다. 임의 모델의 1,024차원 출력만 보고 같은 공간이라고 판단하지 않는다. 기존 blank serving profile 배포는 저장된 계획을 바꾸지 않고 같은 artifact·구성 검사를 통과하면 재사용한다. 등록 종류가 `llm`인 기존 모델도 재사용하되 실제 등록 종류를 응답에 남긴다. 새 배포는 명시적 profile과 `embedding` 또는 기존 `llm` 등록을 허용한다.

## API

| 경로 | 내부 RPC | 동작 |
|---|---|---|
| `GET /api/deployments/{id}/embedding-profile` | `model.embedding.v1.EmbeddingService/GetProfile` | 사용 가능한 배포의 검증된 profile 조회 |
| `POST /api/deployments/{id}/embeddings` | `model.embedding.v1.EmbeddingService/Embed` | query/document 배치 추론 |

POST 예시:

```json
{"expected_embedding_space_id":"GET 응답의 embedding_space_id","role":"query","inputs":[{"id":"question-1","text":"대한민국의 수도는 어디인가?"}]}
```

입력은 접두어를 붙이지 않은 원문이다. Model은 `query`에 `query: `를 한 번 붙이고 `document`에는 아무 접두어도 붙이지 않는다. 공백 제거·Unicode 재정규화·문자열 접두어 추측 제거는 하지 않는다. 원문 자체가 `query: `로 시작하면 원문의 일부로 취급한다. 호출자가 이미 전처리한 문장을 보내는 것은 계약 위반이다.

profile에는 artifact manifest와 tokenizer digest, 확정 revision, 모델 버전, runtime image/container, CLS pooling, L2 정규화, 1,024차원, 역할별 prefix, 처리 한도를 담는다. `embedding_space_id`는 content digest·tokenizer·전처리 계약·runtime image·최대 길이로 산출하며 이름·deployment/container UUID는 제외한다. 동일 artifact/config/image의 재배포는 공간을 유지한다. 이미지나 계약 변경은 보수적으로 다른 공간이다. profile ID는 구현 이름이고 공간 ID는 실제 조합의 식별값이다.

배치는 1~16개, 입력 ID는 중복 없는 1~128 ASCII 식별자, 원문 합계는 128 KiB 이하이다. REST JSON 전체는 1 MiB 이하이다. 입력마다 실제 `/tokenize` 결과의 special token 포함 길이를 검사하고 그 token ID들을 `/v1/embeddings`로 보내므로 전처리·토큰화가 추론 사이에서 달라지지 않는다. 최대 토큰은 배포 `max_model_len`이며 이 profile에서는 최대 8,192까지 허용한다. 현재 사용자 배포는 2,048이다. 자동 자르기·자동 재배치·부분 성공·다른 모델 fallback·자동 재시도는 없다.

응답은 `{profile, role, data}`이며 `data`의 각 항목에 원래 `id`, `index`, `embedding`, `input_tokens`가 있다. runtime 응답이 뒤섞여도 index로 대조해 입력 순서로 반환한다. 개수·중복·범위·모델 alias·차원·유한값·L2 norm 허용오차(0.001)를 모두 검사한다. runtime이 낸 벡터를 임의로 보정하지 않는다.

## 오류와 상태

잘못된 요청/길이 초과는 InvalidArgument, 공간 불일치·준비되지 않은 배포·지원하지 않는 구성은 FailedPrecondition, runtime/조회 실패는 Unavailable, 잘못된 runtime 응답은 DataLoss, 시간 초과는 DeadlineExceeded로 반환한다. REST는 해당 상태를 400/409/503/502/504로 매핑하고 안정된 `EMBEDDING_*` 오류 코드를 제공한다. 전체 RPC 상한은 120초다. profile 조회는 추론을 실행하지 않으며 ready 상태가 이후 요청 성공을 보장하지 않는다.

저장된 runtime 계획의 Docker 소유권·컨테이너·이미지·마운트를 확인하고 인증된 manifest에 있는 작은 구성 파일·tokenizer 파일을 검증한다. 추론 종료 후 배포가 중지/변경됐으면 성공 응답을 내보내지 않는다. 원문과 벡터 전체를 로그나 검사 결과 파일에 저장하지 않는다. 현재 플랫폼의 단일 서버 네트워크/인증 범위는 그대로이며 이 API가 외부 공개용 인증 계층을 추가하지는 않는다.

## 근거

- [모델 설정](https://huggingface.co/dragonkue/snowflake-arctic-embed-l-v2.0-ko/blob/main/config.json), [역할별 prompt](https://huggingface.co/dragonkue/snowflake-arctic-embed-l-v2.0-ko/blob/main/config_sentence_transformers.json), [pooling](https://huggingface.co/dragonkue/snowflake-arctic-embed-l-v2.0-ko/blob/main/1_Pooling/config.json), [modules](https://huggingface.co/dragonkue/snowflake-arctic-embed-l-v2.0-ko/blob/main/modules.json). 웹의 main을 실행 중 identity로 사용하지 않으며 로컬에 고정된 manifest를 검사한다.
- [vLLM 0.15 pooling](https://docs.vllm.ai/en/v0.15.0/models/pooling_models/), [tokenize 계약](https://docs.vllm.ai/en/v0.15.0/api/vllm/entrypoints/serve/tokenize/protocol/). 기존 자동 runner 배포는 계획 변경 없이 사용하고 새 profile은 pooling runner를 명시한다.
