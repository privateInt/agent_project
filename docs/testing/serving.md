# 실제 vLLM 서빙 검증

[서빙 계약](../reference/serving.md)을 검증한다. 기본 source는 기존 Gemma snapshot `cceae526-7b7c-47d4-870b-117372b51885`, GPU는 0이다. 모델 버전이 다른 환경은 `--model-version-id`, GPU는 `--gpu-id`로 지정한다. CPU demo 성공과 실제 GPU 추론 성공을 구분해 기록한다. 학습 adapter를 검증할 때는 [adapter 전용 절차](adapter-serving.md)를 따른다.

## 1. 빌드와 격리 DB 테스트

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python tests/check_build_layout.py
  python -m unittest discover -s tests -p 'test_*.py'
  docker build --target builder -t model-platform-check:serving .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:serving \
    go test -count=1 -v \
      ./deployment/... ./gateway/app ./tests/creation ./dispatcher/... \
      ./reconciler ./operation/app ./internal/operationreport

  docker run --rm model-platform-check:serving \
    go test -count=1 -v ./gc -skip '^TestLiveGCArchiveAndRemoval$'
)
```

DB 테스트는 기존 DB 안의 고유 schema만 생성·정리한다. 위 Deployment/Operation/creation DB 테스트가 SKIP되면 통과로 기록하지 않는다. GPU·Docker 동작은 이 단계에서 fake driver를 사용하며, 실제 Docker/MinIO GC 전체 재검증은 [별도 절차](gc.md)다. 커스텀 네트워크 환경은 `--network`를 같은 이름으로 바꾼다.

새 Go 테스트는 실행 승인 전 무동작, 실제 DB의 준비·중지 상태, Docker create/start 응답 유실, 완료 커밋 실패 복구, 다운로드 중 취소, 다른 소유자 거절, readiness timeout, 종료 후 재시작 방지, Reconciler 완료 결과 보존을 확인한다. Python 테스트는 요청 응답 유실 시 같은 키 사용과 실패 후 중지를 확인한다.

## 2. 이미지 준비와 서비스 전환

검증을 통과한 뒤 실행한다. 새로운 coordinator만 빌드하므로 Axolotl 이미지를 다시 빌드하지 않는다. 기존 import·training·demo Job 이미지는 계속 사용할 수 있다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker buildx bake --builder default -f docker-bake.hcl --load deployment-serving-job
  docker compose build deployment dispatcher reconciler gateway gc

  SERVING_VLLM_IMAGE=$(
    docker compose config --format json |
      jq -er '.services.deployment.environment.VLLM_IMAGE'
  )
  docker pull "$SERVING_VLLM_IMAGE"

  docker compose up -d --no-build --no-deps --force-recreate deployment dispatcher reconciler gc
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose ps
  docker compose logs --tail=40 deployment dispatcher reconciler gateway gc
)
```

Deployment의 `deployment serving controller started`·gRPC·submission worker, Dispatcher와 Reconciler의 시작 로그를 확인한다. GC는 기존 환경의 모드를 유지한다. 새 coordinator 태그 `serving-v1`와 이미지 label `model-platform.deployment-serving=1`이 없으면 실제 서빙 실행을 거절한다. Gateway는 마지막에 전환한다. 구버전 서버로 새 RPC가 전달되면 demo로 대체하지 않고 오류를 반환한다.

## 3. CPU 회귀와 실제 GPU smoke

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_control_smoke.py
python tests/live_serving_smoke.py --gpu-id 0
```

GPU smoke는 다음을 실제 서비스에서 수행한다.

1. ready source와 원본 연결, 로컬 vLLM 이미지, coordinator 이미지 label 확인. 기본 실행의 source는 snapshot이다.
2. POST 전에 키·입력을 `tests/results/serving/<UUID>.json`에 저장.
3. 같은 키로 두 번 POST하여 deployment_id·operation_id가 같은지 확인.
4. 도메인 ready와 Operation succeeded를 기다리고 `/v1/models` 모델명 확인.
5. `/v1/chat/completions`의 실제 응답 모델명이 요청과 같고 비어 있지 않은 assistant 텍스트가 있는지 확인.
6. Stop → stopped → endpoint 제거를 확인. 기존 Operation succeeded는 유지.

스크립트의 기본 대기 시간은 1200초이며 필요하면 `--timeout 2700`까지 설정한다. 실패해도 생성된 배포의 Stop을 시도한다. 중지 확인 실패는 PASS로 처리하지 않는다. 모델 사본과 종료 컨테이너·도메인 행은 남는다. 실제 모델 품질이나 성능을 평가하는 테스트는 아니다.

요청 응답 유실·프로세스 종료 후에는 출력된 결과 경로를 사용해 같은 요청을 재개한다.

```bash
cd ~/workspace/agent_project/model_platform
# 아래 경로를 앞선 실행에서 출력한 실제 JSON 경로로 설정한다.
SERVING_REPORT='tests/results/serving/실제-실행-UUID.json'
python tests/live_serving_smoke.py --resume "$SERVING_REPORT"
```

성공 후 서버를 계속 사용하려면 최초 실행에 `--keep-running`을 추가한다. 실패 시에는 이 옵션이 있어도 중지를 시도한다. 중지된 배포는 같은 키로 재시작하지 않는다. 새로운 서버가 필요할 때는 새 smoke 실행으로 별도 배포를 만든다.

## 진단과 수동 중지

```bash
cd ~/workspace/agent_project/model_platform
API_BASE='http://127.0.0.1:13000'
# 결과 JSON의 실제 deployment_id를 지정한다.
DEPLOYMENT_ID='실제-deployment-id'
curl -fsS "$API_BASE/api/deployments/$DEPLOYMENT_ID" | jq .
docker logs --tail=100 "model-serving-$DEPLOYMENT_ID"
curl -fsS -X POST "$API_BASE/api/deployments/$DEPLOYMENT_ID/stop" | jq .
curl -fsS "$API_BASE/api/deployments/$DEPLOYMENT_ID" |
  jq '{status,apply_status,desired_state,runtime_state,endpoint,runtime_error_code}'
```

status=stopped가 될 때까지 GET으로 확인한다. image unavailable은 pull 여부, preparation failed는 MinIO·manifest·캐시 오류, runtime not running/readiness timeout은 vLLM 로그·GPU 여유를 확인한다. unknown이나 STOP_ID_MISMATCH에서는 다른 컨테이너를 자동 조작하지 않는다. CPU smoke는 통과하더라도 이 GPU smoke가 끝나기 전에는 실제 추론을 검증 완료로 기록하지 않는다.
