# GPU 대기 검증과 기존 요청 관찰

2026-09-09 GPU 대기 수정의 Go·실제 PostgreSQL 테스트, 서비스 이미지 전환, 미시작 요청 복구, 외부 GPU 접근 해제 후 기존 요청의 실제 단일 GPU 학습·새 adapter 등록·vLLM 추론·중지까지 완료했다. 현재 요청에 복구 CLI·이미지 교체·학습 재생성을 반복하지 않는다. 전체 범위는 [현재 상태](../status.md), 경과와 증거는 [개발 이력](../history/model-platform-202609.md#2026-09-09-06-gpu-wait)을 따른다.

| 확인 항목 | 결과 |
|---|---|
| Go·DB | 테스트 82개·하위 사례 41개 PASS, 실패·SKIP 없음, 테스트 패키지 8개 성공 |
| Python | 새 관찰 5개·기존 데이터셋 8개 PASS |
| 서비스 전환 | Operation·Gateway·Dispatcher·Reconciler 적용 |
| 기존 요청 복구 | generation 3·wait 0에서 Job·생성 예약 없음 확인, applied=true |
| 외부 backend | contextrag running/healthy, runtime=runc, gpu_requests=null |
| GPU 대기 후 실행 | wait count 30에서 generation 33으로 진행, 실행 승인 후 학습 완료 |
| 결과물 | adapter 8ee153b0-d333-4fd5-b98e-61de1af7fbc1 ready |
| 실제 서빙 | vLLM ready, 준비 Operation succeeded, adapter 이름으로 Hello! 응답 |
| 멱등·중지 | idempotent_replay=true, stopped=true, 준비 완료 상태 보존 |
| 남은 검증 | 실제 다중 GPU DDP |

실제 학습은 RTX 4090 GPU 0 한 개에서 수행했다. 현재 로그에 다중 rank·NCCL 실행 증거가 없으므로 DDP 성공으로 기록하지 않는다. contextrag의 healthy는 전체 RAG 업무 기능의 CPU 호환성 검증을 대신하지 않는다.

## 완료된 adapter 서빙

새 adapter의 실제 서빙은 완료됐다. 아래 명령은 이후 코드 변경으로 회귀 검증이 필요할 때만 사용한다. 실행할 때마다 새 Deployment를 생성하므로 현재 결과를 확인하려고 반복할 필요 없다. 재검증 시에는 GPU 0의 다른 모델 작업이 종료됐는지 확인한다. contextrag는 GPU 접근 해제 설정으로 실행할 수 있다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_serving_smoke.py \
  --model-version-id 8ee153b0-d333-4fd5-b98e-61de1af7fbc1 \
  --gpu-id 0
```

사용자 서버 로그에서 Deployment ready, 준비 Operation completed/succeeded, adapter 이름으로 채팅 응답, 같은 요청의 멱등 재전송, stopped 뒤 기존 완료 상태 보존을 확인했다. inference.model은 deployment-b3b25017-42b9-4ad3-9d1b-dfb608497878이고 응답은 Hello!다. prompt/completion/total 토큰은 15/3/18이며 품질·부하·장시간 안정성 평가를 수행한 것은 아니다.

중간에 RUNTIME_CREATE_UNCONFIRMED/unknown이 반복됐지만 이후 starting → ready로 진행하고 오류 코드가 해소됐다. 이 관찰만으로 Docker 생성 지연·응답 유실 등 세부 원인을 확정하지 않는다. 최종 PASS 결과와 중간 관찰을 함께 보존한다. 일반 운영 절차는 [adapter 서빙 테스트](adapter-serving.md)를 참고한다.

| 서빙 식별자 | 값 |
|---|---|
| Deployment | b3b25017-42b9-4ad3-9d1b-dfb608497878 |
| Operation | c9b7993b-1f6d-4b65-b4b5-85b7be83a0da |
| Report | tests/results/serving/2c13426a-e444-4c69-ac76-7ce4f496c169.json |
| Runtime container | 9a1ad1c71cb118dd2d22649575501052b37445f8920be141194729e6c5064d52 |
| Runtime image | sha256:59c018a60d965607819387096449351ab9d7ca3134b0a6f7e3f239f33dad9f4b |

보고서의 http://127.0.0.1:32771/v1은 추론 당시 endpoint다. 현재 runtime은 stopped이며 이 주소를 가동 중 서비스 주소로 사용하지 않는다. logs/cache는 보존됐다.

## 완료된 학습의 식별자

| 항목 | 값 |
|---|---|
| DatasetVersion | 30cbca57-555b-4c8f-a53d-cddd7b2b9fcb |
| TrainingRun | 0a4a2aa0-bb2f-4aff-a267-bca30cb92b5a |
| Operation | b32d2d39-de3f-42e1-95f1-44ed777f05b5 |
| Base snapshot | cceae526-7b7c-47d4-870b-117372b51885 |
| Output adapter | 8ee153b0-d333-4fd5-b98e-61de1af7fbc1 |
| Report | tests/results/datasets/592e308e-1352-4256-9550-7811e9ab32b3.json |
| Execution started (UTC) | 2026-09-09T07:44:45.121065Z |

처음 관찰에 남아 있던 GPU_BUSY는 직전 대기 상태였다. 이후 실행 승인부터 완료까지 error_code와 error_message가 비었으며 동일 요청이 succeeded로 끝났다. generation 33은 전달 세대 수이고 gpu_wait_count 30은 별도로 계산된 대기 횟수다. 실행 승인 전 lease 시각이 결과에 남아 있어도 승인 이후의 학습 시간 제한을 의미하지 않는다.

완료 결과를 읽으려면 아래 명령을 사용한다.

```bash
cd ~/workspace/agent_project/model_platform
curl -fsS \
  http://127.0.0.1:13000/api/operations/b32d2d39-de3f-42e1-95f1-44ed777f05b5 |
  jq '{status,phase,error_code,error_message,dispatch_generation,gpu_wait_count,execution_started_at}'
curl -fsS \
  http://127.0.0.1:13000/api/training-runs/0a4a2aa0-bb2f-4aff-a267-bca30cb92b5a |
  jq '{training_run_id,status,output_model_version_id}'
```

## GPU 대기와 10분 관찰의 의미

확정 GPU 점유는 Operation pending/waiting_resources, GPU_BUSY와 점유 컨테이너 이름으로 표시한다. 같은 Outbox 이벤트를 20초 뒤 다시 전달하며 정상 대기만으로 Dispatcher를 종료하지 않는다. 생성 timeout·Docker 조회 실패 등 불확실한 오류는 이 대기로 바꾸지 않는다. 실제 미확인 생성의 한도는 dispatch_generation − gpu_wait_count 기준 최대 3회이며 generation은 계속 증가한다.

이전에 안내한 --timeout 600을 사용한 실제 학습 smoke는 PASS했다. 기본값은 1800초이며 600초는 상태를 기다리는 단계의 관찰 제한이다. 업로드·API 호출과 후속 Operation 완료 확인까지 포함한 전체 명령의 정확한 벽시계 제한은 아니다. 시간 초과가 Job 취소나 실패 확정을 뜻하지 않는다. 아직 완료되지 않은 요청을 이어서 볼 때는 원래 보고서에 --resume을 사용한다.

현재 contextrag는 gpus와 deploy의 두 GPU 요청을 override로 해제했다. 이후 backend 배포에서도 두 Compose 파일을 함께 지정해야 설정이 유지된다. [GPU 접근 운영 안내](../operations/tips.md#external-gpu)에 적용·원복과 확인 경계를 정리했다.

## 코드 변경 시 Go·PostgreSQL 재검증

동일 코드의 아래 테스트는 이미 통과했으므로 현재 진행을 위해 반복할 필요 없다. 이후 대기·실행권·Outbox 계약을 변경할 때 사용하는 명령이다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker build --target builder -t model-platform-check:gpu-wait .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:gpu-wait \
    go test -count=1 -v \
      ./operation/... ./dispatcher/... ./gateway/... ./reconciler \
      ./internal/gpuadmission ./tests/creation
)
```

DB 테스트는 별도 schema를 생성하고 정리한다. 대기/Outbox 원자성, 반복 대기 뒤 실행, 미확인 생성의 3회 한도, 실행 승인 경합, 기존 token 차단, 지연 ACK, 다른 Operation 발행, 동일 event 재전달을 검증했다. Docker inventory를 사용하는 단위 테스트는 fake 관찰이며 실제 서비스 동작 검증과 구분한다.

## 구버전 미시작 요청의 명시적 복구

이 사건의 완료된 Operation에는 다시 적용하지 않는다. 같은 원인의 다른 요청을 복구하려면 먼저 로그에서 Docker 생성 전의 확정 GPU_BUSY를 확인한다. Dispatcher의 `--recover-gpu-wait <Operation UUID>` CLI는 읽기 확인, `--apply`는 증거 확인 후 대기 저장이다. 공유 admission lock 아래 모든 generation의 Job 컨테이너와 생성 예약이 없어야 하며, 만료된 dispatching·실행 승인 없음·현재 token·노드 일치를 요구한다. exited Job이나 미확인 생성 예약이 있어도 거절한다.

복구는 해당 token의 GPU 대기 한 번만 계산하며 과거 실패와 generation을 초기화하지 않는다. 이미 실행 승인된 학습을 재시작하는 기능은 없다. 거절 원인을 확인하며 SQL 초기화·예약 파일 삭제로 우회하지 않는다. [실행 계약](../reference/execution-protocol.md)에 경계를 기록한다.
