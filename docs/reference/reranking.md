# K06 Model reranker 계약

2026-09-21. 구현 전에 확정한 계약이며 현재 소스·화면·검증 도구에 반영했다. [적용·검증 절차](../testing/reranking.md)를 따른다. K05 벡터 검색은 사용자 서버에서 확인했고, 이번 K06은 Model의 배포·추론 capability를 추가한다. Knowledge의 선택적 rerank는 [K07 계약](../knowledge-platform/api-search.md#rerank)에 따라 연결했고 [실제 검색 20→5](../knowledge-platform/acceptance.md#k07)를 확인했다. Knowledge 브라우저 인수는 남아 있다. 사용자 서버의 ready 배포·실제 점수 API와 Playground 화면을 [확인](../knowledge-platform/acceptance.md#k06)했다. 검색 품질은 별도다.

## 책임과 지원 범위

- Model Deployment가 `ko-reranker-v1` profile의 snapshot을 배포하고 질문/후보 점수를 계산한다. 기존 Operation·GPU admission·배포 중지·삭제 경로를 사용한다.
- 첫 대상은 `Dongjin-kr/ko-reranker`, `XLMRobertaForSequenceClassification`, 단일 label, 최대 질문+후보 512토큰이다. 등록 종류 `reranker` 및 기존 `llm` 등록을 명시적 profile로 지원한다. 다른 모델을 이름만 보고 호환 처리하지 않는다.
- 가중치·config·tokenizer는 Management의 고정 artifact에서 검증한다. 배포마다 vLLM image ID를 고정하고 profile identity에 포함한다. 기존 LLM/embedding runtime을 자동 교체하지 않는다.
- 후보 생성·필터·최종 top_k는 Knowledge가 소유한다. Model은 모든 후보의 점수를 입력 순서로 반환한다. 별도 Knowledge 모델 컨테이너, Agent flow, LiteLLM 등록을 추가하지 않는다.

## API

`GET /api/deployments/{deployment_id}/reranker-profile`

ready 배포의 profile 이름·`reranker_profile_id`·artifact/tokenizer/image identity·실행 컨테이너·점수 의미·입력 한도를 조회한다. query/후보 추론이나 새 배포를 수행하지 않는다. 임베딩 공간 ID와 reranker profile ID는 서로 다른 계약이다.

`POST /api/deployments/{deployment_id}/rerank`

```json
{
  "expected_reranker_profile_id": "rp_<64 lowercase hex>",
  "query": "대한민국의 수도는 어디인가?",
  "candidates": [
    {"id": "chunk-1", "text": "대한민국의 수도는 서울이다."},
    {"id": "chunk-2", "text": "사과는 과일이다."}
  ]
}
```

raw UTF-8 질문 하나와 고유 ID를 가진 후보 1~100개를 받는다. 질문+후보 텍스트 합계는 128KiB, HTTP body는 1MiB 이하로 제한한다. null·빈 입력·중복 ID·알 수 없는 필드·모델 URL·top_k는 거절한다. 입력 문자열을 trim/접두어 추가/정규화하지 않는다. 공백뿐인 문자열은 거절한다.

질문과 각 후보를 동일한 고정 tokenizer로 토큰화하고 XLM-R pair 특수 토큰을 포함해 합계 512 이내인지 전체 후보를 먼저 확인한다. 초과한 후보가 하나라도 있으면 추론 결과를 반환하지 않고 전체 요청을 거절한다. 자동 truncation·재시도·후보 제외는 하지 않는다.

응답은 `profile`, `data` 배열이다. 각 data는 `id`, 입력 `index`, 유한 숫자 `score`, `input_tokens`를 포함한다. 배열 순서는 입력 순서이며 backend의 index를 대조해 복원한다. 누락/중복/index 범위 초과·다중 logit·null·NaN/Infinity는 502다. 점수는 단일 classification logit이며 클수록 관련성이 높다. 확률이 아니고 후보 간 softmax·임베딩 점수와의 합산은 하지 않는다.

profile ID에는 고정 artifact·tokenizer·runtime image·입력 pair 정책·점수 처리·토큰 한도를 포함한다. 배포/컨테이너 ID와 변경 가능한 관리 이름은 제외한다. 요청의 expected ID가 현재와 다르면 추론 전에 409로 거절한다. 추론 중 배포 중지/삭제/identity 변경도 결과 성공으로 반환하지 않는다.

## 오류와 시간 제한

| 상태 | 예시 |
|---|---|
| 400 | RERANK_INVALID_REQUEST, RERANK_INPUT_TOO_LARGE, RERANK_INPUT_TOO_LONG, RERANK_INPUT_REJECTED |
| 404 | 배포 없음 |
| 409 | RERANK_NOT_READY, RERANK_UNSUPPORTED_PROFILE, RERANK_PROFILE_MISMATCH, RERANK_IDENTITY_MISMATCH |
| 502 | RERANK_INVALID_RESPONSE |
| 503 | RERANK_UNAVAILABLE |
| 504 | RERANK_TIMEOUT |

profile 조회 20초, 도메인 추론 요청 전체 120초가 상한이다. 신규 API 구현과 검증 도구는 원시 질문·후보 본문·backend 오류 전문을 로그/검증 요약에 보관하지 않는다. 프로세스 API와 실제 GPU 인수는 구분해 기록한다.

## 근거

모델의 [config](https://huggingface.co/Dongjin-kr/ko-reranker/blob/main/config.json)에서 단일 label·XLM-R·max_position_embeddings 514를 확인했고 [모델 카드](https://huggingface.co/Dongjin-kr/ko-reranker)는 질문과 문서의 관련성 점수를 설명한다. 모델 카드의 후보 간 softmax 예제는 플랫폼 API에 채택하지 않는다. [vLLM scoring](https://docs.vllm.ai/en/stable/models/pooling_models/scoring/)은 XLM-R 단일 label scoring과 activation 제어를 문서화한다. 실제 이미지 버전의 API와 pair 처리도 확인하여 구현하며, 지원 모델 표만으로 사용자 GPU 성공을 완료 처리하지 않는다.

현재 기본 이미지 vLLM 0.15.0의 [score protocol](https://github.com/vllm-project/vllm/blob/v0.15.0/vllm/entrypoints/pooling/score/protocol.py), [pair tokenization](https://github.com/vllm-project/vllm/blob/v0.15.0/vllm/entrypoints/pooling/score/utils.py), [usage 합산](https://github.com/vllm-project/vllm/blob/v0.15.0/vllm/entrypoints/pooling/score/serving.py), [pooler activation](https://github.com/vllm-project/vllm/blob/v0.15.0/vllm/config/pooler.py)을 확인했다. `--runner pooling --pooler-config '{"use_activation":false}'`와 요청 `use_activation:false`를 함께 사용한다. `/tokenize`의 개별 무특수토큰 count에 pair 특수토큰 4개를 더하고 `/score`의 usage 합계와 대조해 전처리 불일치를 거절한다.

실제 tokenizer는 `TemplateProcessing`의 `<s> A </s> </s> B </s>`이며 저장된 학습용 LongestFirst/512 설정도 확인했다. 런타임 snapshot 검증은 알려진 pair 구조와 파일 해시를 확인하고 실제 요청은 초과 입력을 거절한다. 단순히 모델 종류나 config의 최대 위치 수만 확인해 지원으로 표시하지 않는다. 파일/정책 검증과 API 대역 검사는 실제 vLLM GPU 실행 성공의 대체가 아니다.
