# 실제 학습 adapter 서빙 검증

snapshot GPU smoke가 이미 완료된 환경에서 [adapter 계약](../reference/adapter-serving.md)을 추가 검증한다. 기본 예제는 기존 Gemma adapter `80e34e1f-f5da-4448-a0ca-282e76d78526`와 그 base `cceae526-7b7c-47d4-870b-117372b51885`다. 최신 완료 여부는 [현재 상태](../status.md)에 기록한다.

## 확인된 기준 실행

2026-09-09 사용자 서버에서 Gemma adapter의 실제 GPU 추론과 중지를 완료했다. 보고서는 `tests/results/serving/b9ee1669-1e81-4065-a646-de7f688682cb.json`, Deployment는 `36ce3251-2b76-4b49-a3df-98a83b977eb8`다. ready → Operation succeeded → adapter 응답 `Hello!` → stopped와 멱등 재요청이 모두 PASS했다.

BOS/EOS 보완 후 deployment/runtime 테스트 22개·하위 사례 24개도 PASS했다. 실제 PostgreSQL 검증은 앞선 03:15의 Go 테스트 43개·하위 사례 32개 결과를 유지한다. 상세 이미지·Operation 식별자와 초기 오류·수정은 [개발 이력](../history/model-platform-202609.md#2026-09-09-03-adapter-serving)에 있다. 완료된 환경에서 아래 테스트·이미지 전환을 다시 실행할 필요는 없다. 이후 변경이나 새로운 환경에서 필요한 범위를 선택해 사용한다.

## 1. 코드와 PostgreSQL 회귀 검증

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python -m unittest discover -s tests -p 'test_*.py' -v
  docker build --target builder -t model-platform-check:adapter-serving .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:adapter-serving \
    go test -count=1 -v \
      ./deployment/... ./gateway/app ./tests/creation ./reconciler ./internal/artifacts
)
```

네트워크를 재정의한 환경에서는 `--network`도 변경한다. DB 테스트는 고유 schema를 만들고 정리한다. **DB 테스트가 SKIP된 실행을 통과로 기록하지 않는다.** 이 단계의 Docker·GPU·Management는 fake이며 실제 모델 호환성을 증명하지 않는다.

Go 테스트는 원본 연결·rollback·멱등성·Stop, 준비 실패 시 생성 차단, 저장된 embedding·출력층·LoRA·tokenizer 옵션 보존, 미지원 입력·손상된 캐시·취소 처리, format 1 및 snapshot 호환성을 확인한다. Gateway는 source_type·계보의 실제 HTTP 응답을 검사한다. Python suite 24개에는 base-only 등록·잘못된 응답 모델·원본 연결 오류를 실패로 판정하고 중지하는 사례가 있다. 옵션 검증만 수정하는 경우 deployment/runtime 단위 테스트를 선택하고, DB·상태 계약 변경은 위 통합 테스트를 포함한다.

## 2. 변경한 서비스 이미지 교체

앞 단계가 통과한 뒤 실행한다. 아래는 Deployment·Gateway를 함께 수정했을 때의 명령이다. Deployment만 변경했으면 build/up 대상도 deployment로 한정한다. 기존 vLLM 이미지와 `deployment-serving-job:serving-v1`를 사용한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build deployment gateway
  docker compose up -d --no-build --no-deps --force-recreate deployment
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose ps deployment gateway
  docker compose logs --tail=40 deployment gateway
)
```

Deployment의 gRPC·submission worker·serving controller 시작을 확인한다. Dispatcher·Reconciler·GC·Axolotl 재빌드와 새 vLLM pull은 이번 변경에 필요하지 않다. Compose와 GC 모드도 변경하지 않는다. adapter 배포가 남아 있으면 새 Deployment가 계속 관리해야 한다. rollback 주의점은 [계약](../reference/adapter-serving.md#배포-호환성과-한계)에 있다.

## 3. 기존 adapter로 실제 GPU 추론

GPU 0에서 다른 학습이나 서빙이 실행 중이지 않은 상태에서 실행한다. 기준 adapter smoke도 기본 동작으로 서버를 중지했다. 다른 GPU를 사용하면 `--gpu-id`를 바꾼다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_serving_smoke.py \
  --model-version-id 80e34e1f-f5da-4448-a0ca-282e76d78526 \
  --gpu-id 0
```

별도 smoke 스크립트를 복제하지 않고 기존 도구를 확장했다. source가 adapter이면 자동으로 원본 연결 검사를 수행한다. 실제 검증 순서는 다음과 같다.

1. adapter·base 조회와 이미지 사전 검사.
2. 결과 파일에 키·입력·기대 원본 연결 저장 후 같은 키로 두 번 POST.
3. adapter 준비 → vLLM ready → Operation succeeded 확인.
4. 조회된 adapter/base ID와 vLLM adapter alias·parent·root 대조.
5. adapter 이름으로 채팅 요청, 응답 모델 이름과 비어 있지 않은 답변 확인.
6. Stop → stopped 확인, 기존 Operation succeeded 보존 확인.

결과는 `src/platform/model/tests/results/serving/<UUID>.json`에 남는다. 기본으로 성공·실패 모두 중지를 시도하며, 성공한 서버만 유지하려면 최초 실행에 `--keep-running`을 붙인다. 캐시·종료 컨테이너·원본 학습 결과는 남는다. base와 같은 답변이어도 정상일 수 있고, 이 검증은 품질 평가가 아니다.

다운로드·모델 준비가 오래 걸리는 환경은 `--timeout 2700`을 사용한다. controller의 준비 제한 30분과 시작 후 readiness 제한 15분은 별도다. 응답 유실이나 중단 후에는 새 키로 중복 실행하기 전에 기존 보고서를 재개한다.

```bash
cd ~/workspace/agent_project/model_platform
# 앞선 실행이 출력한 실제 JSON 경로로 바꾼다.
SERVING_REPORT='tests/results/serving/실제-실행-UUID.json'
python tests/live_serving_smoke.py --resume "$SERVING_REPORT"
```

이미 stopped로 기록된 배포는 재시작하지 않는다. 새 서빙이 필요할 때 새 요청을 만든다. CPU 제어 경로 재확인이 필요하면 기존 `python tests/live_control_smoke.py`를 별도로 실행한다.

## tokenizer 차이 진단

format 1의 `adapter tokenizer changed added_tokens`는 엄격한 비교 실패다. 이번 실제 진단에서는 배열 순서가 아니라 ID 4 `<mask>`의 special=false→true와 embedding 262144→262145행을 확인했다. 다른 결과물의 원인은 아래 도구로 확인한다. added token의 normalized·special·공백 처리 옵션은 토큰화·디코딩에 영향을 줄 수 있다. [Hugging Face AddedToken 설명](https://huggingface.co/docs/tokenizers/api/added-tokens)

실패한 배포는 원본 캐시를 유지한다. 다음 도구는 실행 중인 Deployment 서비스 안의 캐시를 읽으며 모델 파일을 변경하거나 GPU 컨테이너를 만들지 않는다. Go·Docker 이미지 재빌드 없이 호스트 Python 표준 라이브러리와 Docker CLI로 실행한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/inspect_serving_adapter.py \
  --deployment-id e1a7a9a7-1b9e-41e8-a8d2-cc6a36580caf
```

다른 배포를 조사하면 실제 ID로 바꾼다. Deployment 컨테이너 이름을 변경한 환경은 `--container`를 지정한다. 이 도구는 `/serving-cache/<deployment_id>/model/`과 `adapter-source/`의 파일을 읽는다.

- `added_tokens`: ID별 추가·삭제, 옵션별 변경 수, 원본/학습 결과의 실제 레코드. 터미널에는 변경 사례 20개까지, JSON에는 전체를 저장한다.
- `only_array_order_differs`: ID별 레코드가 같고 중복 ID 없이 배열 순서만 다른 경우를 표시한다. 이 표시만으로 tokenizer 전체의 동작 동일성을 확정하지 않는다.
- `tokenizer_config`: BOS/EOS/PAD 등 주요 설정과 필드 존재 여부.
- `weight_headers`: base/adapter embedding의 dtype·shape. safetensors header만 읽으며 tensor 값은 비교하지 않는다.

출력 마지막 `[report]`의 `tests/results/serving/<deployment_id>-inspect-<UUID>.json`을 확인한다. 모델 파일·tokenizer 전체를 별도로 업로드할 필요 없이 이 보고서로 다음 수정 범위를 판단한다. 실제 모델 값 비교가 필요하면 별도 단계로 진행한다. 진단 실패나 shape 일치만으로 서빙 성공을 기록하지 않는다.

위 진단 명령의 ID는 초기 실패 배포이며 stopped 상태다. 조사할 배포에 맞춰 ID를 바꾸고, 원인 수정 뒤에는 새 smoke 실행으로 새 배포를 만든다. 멱등 키를 재사용해 중지된 배포를 다시 시작하지 않는다.

## 4. 진단과 검증 기록

```bash
cd ~/workspace/agent_project/model_platform
API_BASE='http://127.0.0.1:13000'
# 실제 smoke 결과의 deployment_id로 바꾼다.
DEPLOYMENT_ID='실제-deployment-id'
curl -fsS "$API_BASE/api/deployments/$DEPLOYMENT_ID" |
  jq '{status,apply_status,artifact_kind,base_model_version_id,adapter_model_version_id,served_model_name,base_served_model_name,runtime_error_code,runtime_error_message}'

docker exec model-deployment \
  cat "/serving-cache/$DEPLOYMENT_ID/adapter/serving-adapter.json" | jq .
```

receipt는 준비 완료 후에 존재한다. format 2에서는 실행용 base의 증거도 함께 확인한다.

```bash
docker exec model-deployment \
  cat "/serving-cache/$DEPLOYMENT_ID/adapter-base/serving-base.json" | jq .

docker exec model-deployment \
  cat "/serving-cache/$DEPLOYMENT_ID/adapter-base/config.json" |
  jq '{model_type,vocab_size,hidden_size,tie_word_embeddings}'
```

이번 Gemma 결과물의 기대값은 serving_format=2, tokenizer_source=adapter, restored_vocab_size=262145, preserved_embeddings에 model.embed_tokens.weight/lm_head.weight 두 개, config의 tie_word_embeddings=false다. LoRA receipt의 lora_tensor_count 기대값은 364다. 실제 출력으로 확인하며, receipt 생성 자체가 GPU 추론 성공을 증명하지 않는다.

format 2의 verified_duplicate_embeddings는 adapter-base와 수치가 같은 이름이다. 원본 snapshot과의 동일성을 뜻하지 않는다. LoRA 사본은 full embedding을 별도 base로 옮겨 작아지지만 전체 서빙 디스크 사용량은 증가한다.

| 오류 | 확인할 내용 |
|---|---|
| ADAPTER_PREPARATION_FAILED | 함께 반환된 message의 manifest·파일 해시·tokenizer·LoRA 옵션·tensor 검사 실패 |
| saved embedding differs from base | format 1 또는 format 2의 미지원 모델·불완전 saved matrix 경로. format과 두 full matrix, 모델 구조 확인 |
| adapter tokenizer changed / unsupported saved tokenizer change | format과 표시 필드 확인. format 2의 Gemma 경로도 token ID·내용·tokenizer.json 규칙 변경은 거절. add_bos_token/add_eos_token은 저장된 boolean·생략을 보존 |
| RUNTIME_NOT_RUNNING / READINESS_TIMEOUT | `docker logs --tail=100 "model-serving-$DEPLOYMENT_ID"`에서 vLLM LoRA 로드·GPU 메모리 오류 확인 |
| 모델 목록/응답 이름 검사 실패 | 새 Gateway·Deployment 이미지 적용과 adapter alias·parent·root 확인 |

오류를 우회하려고 원본 tensor를 삭제하거나 adapter를 snapshot으로 재표기하지 않는다. 준비 단계 실패는 GPU 컨테이너를 만들지 않는다. vLLM 시작 후 실패는 smoke의 Stop 결과를 확인한다. 수동 중지가 필요하면 [공통 중지 절차](serving.md#진단과-수동-중지)를 사용한다.

PASS/SKIP, 이미지 ID, source/base/adapter ID, deployment/Operation ID, 실제 응답과 stopped 여부를 남긴다. 실제 서버 로그를 확인하기 전에는 adapter GPU 검증 완료로 표시하지 않는다.
