# 로컬 DDP 학습과 GPU 점유 관리

현재 구현은 **한 Docker 호스트의 QLoRA DDP 학습**과 GPU 생성 예약 관리다. 학습 Job은 기존 GPU 점유가 있으면 기다리며, 서빙은 2026-09-16 변경 이후 다른 사용자가 있어도 공유 로딩을 시도한다. 구현·검증 상태는 [현재 상태](../status.md), 적용 명령은 [테스트](../testing/multi-gpu.md)를 따른다. 여러 서버의 GPU를 묶는 기능은 포함하지 않는다.

## 요청과 결과

`POST /api/training-runs`의 기존 모델·데이터셋 필드를 유지하고 아래 필드를 사용한다.

```json
{
  "source_model_version_id": "cceae526-7b7c-47d4-870b-117372b51885",
  "dataset": {
    "source_type": "version",
    "dataset_version_id": "37cf7472-3269-4075-bde8-8369df60ba87"
  },
  "training": {
    "method": "qlora",
    "strategy": "ddp",
    "max_steps": 1,
    "micro_batch_size": 1,
    "gradient_accumulation_steps": 1
  },
  "resources": {"gpu_ids": [0, 1]}
}
```

위 UUID는 이전 서버 검증에서 등록한 리소스다. `[0,1]`은 **같은 호스트에 실제 존재하는 GPU 두 개**를 뜻한다. 한 장씩 있는 서로 다른 서버를 뜻하지 않는다.

| 항목 | 계약 |
|---|---|
| GPU 1개 | strategy 생략 또는 `single`. 기존 명령·요청 키 계산 유지 |
| GPU 2..16개 | 정확한 gpu_ids 필수. strategy 생략 시 `ddp`로 고정 |
| GPU IDs | 중복·음수 거절, 오름차순 정규화. gpu_count를 함께 주면 개수 일치 필수 |
| 지원하지 않는 조합 | count만 지정한 다중 GPU, 한 GPU의 ddp, 다중 GPU의 single, fsdp/deepspeed |
| node_name | Dispatcher의 로컬 노드 이름과 일치해야 함. 원격 라우팅 기능 없음 |
| 유효 batch | micro_batch_size × gradient_accumulation_steps × world_size |
| 실행 후 재시작 | torchrun max-restarts=0. 승인 후 실패한 학습은 자동 재실행하지 않음 |

여기서 16은 API 상한이며 해당 수의 GPU에서 실행·성능을 검증했다는 의미가 아니다. GPU 개수와 메모리는 사용자가 실제 호스트에서 확인한다. DDP는 GPU마다 모델 사본을 사용하므로 여러 GPU의 메모리를 하나로 합치는 방식이 아니다.

`GET /api/training-runs/:id`의 `.training.strategy`는 다중 GPU에서 ddp다. 단일 GPU의 정규화 값은 빈 문자열이며 이전 fingerprint를 유지한다. 새 Job 결과에는 `.result.parallelism`을 추가한다.

```json
{
  "strategy": "ddp",
  "world_size": 2,
  "effective_batch_size": 2,
  "ranks": [
    {"rank": 0, "local_rank": 0, "world_size": 2, "training_run_id": "<run UUID>", "device_uuid": "<GPU UUID>", "initialized": true, "ddp_wrapped": true, "completed": true, "global_step": 1},
    {"rank": 1, "local_rank": 1, "world_size": 2, "training_run_id": "<run UUID>", "device_uuid": "<different GPU UUID>", "initialized": true, "ddp_wrapped": true, "completed": true, "global_step": 1}
  ]
}
```

명령줄에 GPU 수만 기록한 결과로는 성공하지 않는다. 각 worker가 같은 NCCL group에 참여했는지 collective로 확인한 뒤, Trainer의 실제 DDP wrapper·rank·완료 step·CUDA 장치 UUID를 기록한다. 모든 rank의 파일을 검증한 후에만 결과물을 업로드한다. Training의 최종 완료 처리에서도 저장된 GPU 수·step·batch·TrainingRun ID와 다시 대조한다. 누락·중복 rank, 같은 장치 재사용, 단일 GPU로 실행된 결과를 거절한다.

## 실행 구조

단일 GPU의 `axolotl train` 경로는 유지한다. 다중 GPU는 Job의 Go 부모 프로세스가 한 번의 데이터셋 검증·preprocess를 마친 다음 아래 구조로 실행한다.

```text
python -m torch.distributed.run --standalone --nnodes=1 --nproc-per-node=N --max-restarts=0 /app/distributed_train.py ...
```

worker는 Axolotl module CLI를 호출하므로 launcher가 중첩되지 않는다. host GPU IDs는 Docker에서 선택하며, worker에는 컨테이너 내부 LOCAL_RANK를 적용한다. 예를 들어 host `[1,3]`은 내부 `cuda:0, cuda:1`을 사용한다. preprocess에는 별도 프로세스 환경으로 CUDA_VISIBLE_DEVICES=0을 설정한다. DDP 설정은 ddp_find_unused_parameters=false, gradient checkpointing use_reentrant=false이며 FSDP/DeepSpeed 설정은 넣지 않는다.

[Axolotl 다중 GPU 안내](https://docs.axolotl.ai/docs/multi-gpu.html)와 [CLI 안내](https://docs.axolotl.ai/docs/cli.html)의 DDP·module CLI를 참고했다. 실행 이미지는 기존 `axolotlai/axolotl:0.18.0`을 유지한다. 문서 확인은 이 이미지와 사용자 GPU에서의 실제 실행 검증을 대신하지 않는다.

## GPU 점유 관리

Dispatcher와 Deployment controller는 같은 로컬 Docker named volume `model-gpu-admission`을 `/gpu-admission`에 마운트한다. `internal/gpuadmission`은 flock을 잡고 Docker 상태 조회 → 충돌 검사 → 예약 기록 fsync → 생성 호출을 직렬화한다. 일반 CPU Job과 deployment.serve 제어용 Job은 GPU를 점유하지 않는다.

| 관찰 | 처리 |
|---|---|
| running / paused / restarting / created | 학습 Job의 충돌 판단에 GPU 점유 유지; 공유 서빙은 점유만으로 대기시키지 않음 |
| exited | 다음 관찰에서 점유 해제, 컨테이너·로그 보존 |
| 이전에 확인한 컨테이너가 제거됨 | 다음 관찰에서 점유 해제 |
| 생성 응답 유실, 아직 컨테이너 미확인 | 예약 유지. 시간 경과만으로 해제하지 않음 |
| Docker 조회 실패·예약 파일 손상 | 새 GPU 생성 차단 |
| 정확한 ID가 겹치지 않음 | 병행 실행 가능 |
| UUID/MIG·count만 있는 외부 Docker 요청 | host index와 안전하게 비교할 수 없어 전체 GPU 점유로 취급 |
| 단일 GPU의 기존 count-only 요청 | 호환성을 유지하되 전체 GPU를 보수적으로 예약 |

같은 Operation의 오래된 dispatch token이 실행 승인 전에 무효화되고 컨테이너도 보이지 않으면 새 token으로 예약을 넘길 수 있다. 이전 Job의 실행은 기존 BeginExecution 계약이 차단한다. 실행 승인 후 학습 재시작 기능을 추가한 것은 아니다.

서빙은 `Shared=true`로 생성/시작하며 GPU 점유만으로 `waiting_resources/GPU_BUSY`를 반복하지 않는다. 예약 장부·소유권·Docker 조회 오류·생성 취소 방어는 유지한다. 로딩 실패는 [서빙 오류 계약](serving.md#같은-gpu의-여러-서빙-모델)에 따라 종료하며 자동 재시작하지 않는다. 이 동작은 VRAM 여유를 보장하지 않는다. 학습은 TrainingRun=created, Operation=pending/waiting_resources, error_code=GPU_BUSY로 대기한다. 확정 점유를 DB와 Outbox에 함께 저장하고 20초 후 동일 이벤트를 재전달한다. 대기만으로 실행 전 실패 한도를 소모하거나 Dispatcher를 재시작하지 않으며 같은 Kafka partition의 후속 요청도 처리한다. 실제 불확실한 생성은 기존 lease·Reconciler와 3회 한도를 유지한다. 실행권 generation은 단조 증가하고 GPU 대기를 뺀 횟수에 한도를 적용한다. 자세한 계약은 [실행 프로토콜](execution-protocol.md), 구버전의 정체된 요청 복구는 [GPU 대기 수정](../testing/gpu-wait.md)을 따른다. 선점·공정성·우선순위 스케줄링은 없다. 로컬 학습의 대기/실행 중 취소는 [학습 제어](training-control.md)를 따른다.

서빙 Stop은 durable desired_state=stopped를 확인하고 실제 중지 후 예약을 해제한다. 아직 생성도 확인되지 않은 예약도 이때 해제한다. 늦게 나타난 created 컨테이너는 stopped controller가 시작하지 않으며 다음 관찰에서 제거한다. **한 번도 시작하지 않은 created 컨테이너만 소유권을 확인해 force 없이 제거**하고, 실행 후 종료된 컨테이너와 모델 캐시는 보존한다. 해제 확인이 실패하면 `GPU_RELEASE_UNCONFIRMED`와 stopping을 유지한다.

플랫폼 밖에서 Docker 컨테이너를 직접 시작하거나 호스트에서 CUDA 프로그램을 실행하는 작업까지 잠금으로 통제하지 않는다. 다른 Docker daemon·Kubernetes·다른 호스트와도 예약을 공유하지 않는다. 외부 작업은 시작 전에 별도로 확인한다. 이 볼륨을 NFS/JuiceFS로 바꾸거나 pending/running 요청이 있는 동안 삭제하지 않는다.

관련 구현은 [admission](../../model_platform/internal/gpuadmission/admission.go), [Dispatcher](../../model_platform/dispatcher/executor/docker.go), [controller](../../model_platform/deployment/app/serving_controller.go), [worker](../../model_platform/jobs/training/axolotl/distributed_train.py), [결과 검증](../../model_platform/internal/trainingparallel/evidence.go)에 둔다.
