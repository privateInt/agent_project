# 로컬 DDP·GPU 점유 관리 적용과 검증

2026-09-09 사용자 서버에서 **Go 테스트 89개·하위 사례 70개가 모두 PASS**했고 실패·SKIP은 없었다. 실제 PostgreSQL의 DDP 요청·완료 계약과 기존 학습·서빙 회귀도 통과했다. 작성 환경의 관련 Python 18개(새 DDP 6개·기존 데이터셋 8개·기존 서빙 4개)는 별도 통과 결과다. **이후 GPU 대기 수정의 Go 82개·하위 사례 41개, 기존 요청 복구와 단일 GPU 실제 학습·adapter 등록·vLLM 추론·중지까지 통과했다.** [연결 검증 결과](gpu-wait.md)에 기록했다. 실제 DDP 실행은 아직 검증하지 않았다. Python worker의 torch/Trainer와 Go의 Docker 관찰은 fake이며 실제 NCCL·CUDA 학습 성공을 의미하지 않는다. [계약](../reference/multi-gpu.md), [상태](../status.md)를 함께 확인한다.

## 적용과 Go·PostgreSQL 테스트

최초 DDP 코드의 아래 Go·DB 테스트는 이미 통과했다. 뒤이은 GPU 대기 수정의 별도 Go·DB 검증과 Operation·Gateway·Dispatcher·Reconciler 교체도 완료됐다. [현재 검증 단계](gpu-wait.md)를 따른다. 아래 명령은 새 환경 적용 또는 이후 변경 시 재검증용이다. Compose/Bake 결과는 이번 첨부에 없으므로 기존 실행에서 PASS했는지 확인하고, 실행하지 않았다면 `python tests/check_build_layout.py`를 먼저 실행한다.

아래 재검증 명령은 최신 소스가 반영된 agent_project에서 사용한다. 최초 DDP 패치 이후 GPU 대기 수정에는 Operation 스키마 추가가 포함된다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python -m unittest discover -s tests -p 'test_multi_gpu.py' -v
  python -m unittest discover -s tests -p 'test_live_dataset_smoke.py' -v
  python -m unittest discover -s tests -p 'test_live_serving_smoke.py' -v
  python tests/check_build_layout.py

  docker build --target builder -t model-platform-check:local-ddp .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:local-ddp \
    go test -count=1 -v \
      ./internal/gpuadmission ./internal/trainingparallel \
      ./training/... ./gateway/... ./dispatcher/... \
      ./deployment/... ./jobs/training/axolotl ./tests/creation
)
```

PostgreSQL 검증은 연결 DB의 고유 schema를 생성하고 정리한다. URL이 없어서 SKIP된 결과는 통과로 처리하지 않는다. GPU 점유 단위 테스트는 실제 flock·파일 저장을 사용하고 Docker 관찰은 fake다. 생성 경합·응답 유실·재기동·외부 점유·종료/삭제·생성 전 중지·소유권 불일치·손상 상태를 확인한다. REST/DB 검증은 DDP 요청 정규화·멱등 재요청과 일부 rank만 있는 완료 결과의 거절을 포함한다.

## 이미지 빌드와 전환

테스트 후 네 서비스와 학습 Job 이미지를 빌드한다. 이 단계만으로 GPU 학습은 시작되지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build training gateway deployment dispatcher
  docker buildx bake --builder default -f docker-bake.hcl --load training-job
  docker image inspect model-platform-training-axolotl:launch-v1 \
    --format '{{index .Config.Labels "model-platform.training-ddp"}}' |
    grep -qx 1
)
```

기존 GPU Job이 종료됐고 서빙이 stopped이며 새 요청을 제출하지 않는 시점에 아래 전환을 실행한다. 기존 Dispatcher는 새 잠금 코드를 사용하지 않으므로 **먼저 중지하고 마지막에 새 이미지로 기동**한다. 이전 pending 요청도 시작될 수 있으므로 전환 전 기존 요청 상태를 확인한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose stop dispatcher
  docker compose up -d --no-build --no-deps --force-recreate training gateway deployment
  docker compose up -d --no-build --no-deps --force-recreate dispatcher
  docker compose ps training gateway deployment dispatcher
  docker compose logs --tail=30 training gateway deployment dispatcher
)
```

위 네 서비스 목록은 최초 DDP 추가의 범위다. 후속 GPU 대기 수정에서 Operation·Gateway·Dispatcher·Reconciler를 추가로 교체했고 현재 서버에 적용됐다. protobuf strategy는 추가 필드이며 생성 코드는 builder에서 만든다. 새 shared volume은 자동 생성한다. 기존 vLLM 런타임 이미지 태그·캐시는 유지한다.

공유 마운트와 실제 GPU 목록을 확인한다. Docker inspect에서는 환경 변수나 비밀 값을 출력하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
for GPU_SERVICE_CONTAINER in model-operation-dispatcher model-deployment; do
  docker inspect --format \
    '{{.Name}} {{range .Mounts}}{{if eq .Destination "/gpu-admission"}}{{.Name}} {{.Destination}}{{end}}{{end}}' \
    "$GPU_SERVICE_CONTAINER"
done
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used --format=csv
```

두 서비스 모두 `model-gpu-admission /gpu-admission`이 보여야 한다. 메모리 여유·실행 중 작업·MinIO 파일시스템 공간도 확인한다. 이전 MinIO 오류는 모델 코드가 아니라 같은 SSD의 여유 공간 부족에서 발생했다. 이번 빌드로 캐시가 다시 늘어날 수 있다.

## GPU 한 개로 기존 경로 회귀

현재 호스트의 GPU 0에서 기존 요청의 학습·adapter 등록·실제 서빙·추론·중지까지 [완료했다](gpu-wait.md). 아래는 이후 코드 변경으로 새 요청을 검증해야 할 때 사용하는 명령이며 현재 결과를 위해 완료된 학습을 반복할 필요 없다. DDP 테스트를 위해 존재하지 않는 GPU 1을 지정하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_dataset_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --gpu-id 0
```

등록·단일 GPU 1 step·결과물 등록을 실제로 실행한다. PASS 후 출력된 output_model_version_id로 [adapter 서빙 smoke](adapter-serving.md)를 실행해 준비·추론·중지와 GPU 점유 해제를 확인한다. 같은 GPU의 요청은 순서대로 제출한다. 기본 등록 전용 smoke는 지난 단계에서 이미 검증했으므로 다시 실행할 필요 없다.

## 같은 호스트의 GPU 두 개로 DDP 검증

같은 호스트에 사용 가능한 GPU 0과 1이 실제로 존재할 때만 실행한다. 다른 번호를 사용할 때는 `--gpu-ids` 뒤 값을 바꾼다. 두 개 이상의 rank에서 1 step을 수행하며, 한 rank만 학습하면 실패해야 한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_dataset_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --gpu-ids 0 1
```

보고서는 `tests/results/datasets/<run UUID>.json`이다. 성공 후 rank별 증거를 확인한다. 아래 GPU_DDP_REPORT에는 출력된 `[record]` 경로를 넣는다.

```bash
GPU_DDP_REPORT=tests/results/datasets/실제-run-UUID.json
jq '{passed,training_passed,parallelism,output_model_version_id}' "$GPU_DDP_REPORT"
```

기대값은 passed=true, training_passed=true, strategy=ddp, world_size=2, effective_batch_size=2, 서로 다른 장치의 rank 0·1 모두 global_step=1이다. smoke가 이를 자동 검사한다. 결과 adapter는 다른 rank별 모델 여러 개가 아니라 하나로 등록하며 기존 단일 GPU vLLM에서 검증한다.

```bash
python tests/live_serving_smoke.py \
  --model-version-id 실제-output-model-version-ID \
  --gpu-id 0
```

DDP 학습 프로세스가 성공했더라도 MinIO 업로드·등록이나 서빙이 실패하면 전체 연결 검증을 완료 처리하지 않는다. 중단·응답 유실 후 관찰은 원래 보고서로 이어 간다.

```bash
python tests/live_dataset_smoke.py --resume tests/results/datasets/실제-run-UUID.json
```

resume에 새 GPU IDs를 지정하지 않는다. 실패한 실행은 재시작하지 않으며 원인을 수정한 후 새 요청을 만든다. 위 경로는 예시 자리표시자이므로 실제 출력 값으로 바꾼다.

## 점유·실패 진단

서빙 API의 GPU_BUSY는 실패 확정이 아니라 다른 GPU 사용자의 종료를 기다리는 상태다. 수정 후 학습 대기는 TrainingRun=created / Operation=pending, phase=waiting_resources다. GET Operation의 GPU_BUSY·error_message와 Dispatcher 로그를 함께 본다. 구버전에서 dispatching에 남고 launch 한도를 소진한 요청은 [명시적 복구](gpu-wait.md)를 따른다. 리소스 확인을 위해 같은 요청을 새 키로 반복 제출하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose logs --tail=80 dispatcher deployment
docker compose exec -T dispatcher cat /gpu-admission/claims.json
docker ps -a --filter label=model-platform.owner=operation-dispatcher \
  --format '{{.ID}} {{.Names}} {{.Status}}'
```

claims.json은 첫 GPU 생성 또는 서빙 Stop 관찰 때 만들어진다. 아직 파일이 없으면 요청 이력과 서비스 기동부터 확인한다. 파일 내용은 컨테이너 이름·Operation ID·GPU ID·관찰 여부이며 credentials는 저장하지 않는다. 불확실한 생성의 예약을 수동 삭제하면 늦게 생성되는 컨테이너와 GPU가 겹칠 수 있으므로, 해당 컨테이너와 Operation 상태부터 확인한다.

| 증상 | 확인 |
|---|---|
| 새 Job 이미지 DDP 미지원 | training-job 빌드와 training-ddp=1 label 확인 |
| visible GPU 수 / LOCAL_RANK 불일치 | 같은 호스트의 실제 ID, Docker GPU 전달, 컨테이너 내부 장치 수 확인 |
| NCCL timeout / DDP worker 실패 | 각 rank 로그·GPU 연결·메모리 확인. 단일 GPU 실행을 다중 GPU 성공으로 대체하지 않음 |
| DDP_EVIDENCE_INVALID | 일부 rank 파일 유실·중복 장치·step 불일치. Job은 이 시점에 output을 등록하지 않음 |
| GPU_BUSY | 기존 created/running 컨테이너 또는 미확인 예약의 소유 요청 확인 |
| GPU_RELEASE_UNCONFIRMED | desired_state=stopped 이후 실제 종료·예약 파일·Docker 조회 확인 |
| 재기동 후 모든 GPU 생성 실패 | 두 서비스의 공유 볼륨·권한·claims.json 형식·Docker 접근 확인 |

최초 DDP 코드의 Go·실제 PostgreSQL 검증과 이후 네 서비스 기동은 사용자 서버 로그로 확인했다. GPU 대기 수정의 Go·DB·실제 복구와 수정 적용 후 한 GPU 학습·adapter 등록·실제 vLLM 추론·중지까지 완료했다. 실제 DDP는 같은 호스트에 GPU 두 개 이상인 환경에서 검증해야 한다. 각 결과를 따로 기록하며 기존 단일 GPU 성공을 DDP 실행의 근거로 사용하지 않는다.
