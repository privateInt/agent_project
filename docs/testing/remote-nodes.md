# 원격 노드 학습 검증

현재 진행 상태: 2026-09-09 사용자 요청에 따라 **단일 서버 기능 완성을 우선하고 실제 원격 노드 준비·검증은 보류**한다. Kubernetes 전환은 프로젝트 완성 뒤의 단계다. 아래 절차는 원격 작업 재개 시 참고할 문서이며, 지금 실행할 필수 단계가 아니다. 최신 작업 순서는 [현재 상태](../status.md)를 따른다.

이번 단계는 **simsys4090에서 요청하고 simsys4070의 GPU 0에서 단일 GPU 학습을 실행하는 경로**다. `resources.node_name`으로 배치를 지정한다. 자동 노드 선택, 다중 노드 DDP/FSDP, arm64/GB10 학습, 원격 vLLM 배포는 이번 구현 범위에 포함되지 않는다. 노드를 생략한 기존 로컬 실행은 유지한다.

2026-09-09 로그에서 **Go 100개·하위 사례 41개 PASS, 실패·SKIP 없음**, 중앙 서비스 3개 이미지 교체·기동, 실제 MinIO 등록 smoke를 확인했다. **1단계는 완료했다.** 실제 SSH 연결·원격 GPU 학습은 아직 검증하지 않았다. 재개 시 대상 환경과 그동안의 코드 변경을 확인한 뒤 남은 절차를 진행한다.

## 1. simsys4090: 적용·Go/DB 테스트 — 완료

`agent_project`에 tar를 업로드한 뒤 실행한다.

```bash
cd ~/workspace/agent_project/model_platform

(
  set -euo pipefail
  docker build --target builder -t model-platform-check:remote-node .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:remote-node \
    go test -count=1 -v \
      ./internal/remotenode ./internal/gpuadmission \
      ./dispatcher/... ./training/... ./gateway/app \
      ./operation/app ./reconciler ./tests/creation
)
```

새 테스트는 노드 이름·daemon ID·실행 권한 불일치, SSH 응답 유실, 원격 GPU 대기, 중복 실행 요청, 로컬로 잘못 실행되는 경우, 노드 비활성화 후 기존 요청의 멱등 응답, 원격 컨테이너 관찰을 검사한다. PostgreSQL 테스트가 SKIP이면 완료로 기록하지 않는다.

통과 후 중앙 이미지 3개를 교체한다. 아직 원격 설정을 활성화하지 않는 로컬 호환 단계다.

```bash
(
  set -euo pipefail
  docker compose build training dispatcher reconciler
  docker compose stop dispatcher
  docker compose up -d --no-build --no-deps --force-recreate training reconciler
  docker compose up -d --no-build --no-deps --force-recreate dispatcher
  docker compose ps training dispatcher reconciler
  docker compose logs --tail=40 training dispatcher reconciler
  python tests/live_dataset_smoke.py
)
```

기존 로컬 GPU 학습을 반복할 필요는 없다. 다음 절차로 원격 호스트를 준비한다.

## 2. simsys4070: 실행 환경 확인

이 문서는 이전에 확인한 `simsys@192.168.0.173`을 예제로 사용한다. 주소·계정이 현재와 다르면 SSH 설정과 아래 명령을 실제 대상에 맞춘다. 이 작업에서 다른 서버나 ContextRAG를 변경하지 않는다.

```bash
ssh simsys@192.168.0.173
uname -m
docker info --format 'id={{.ID}} arch={{.Architecture}} os={{.OSType}}'
nvidia-smi
mkdir -p ~/workspace/model_platform_worker
exit
```

Linux `x86_64`와 사용 가능한 NVIDIA GPU가 필요하다. 로그인 계정에서 `docker`를 비대화식으로 실행할 수 있어야 한다. 기존 Kubernetes/containerd 설정이나 드라이버는 변경하지 않는다. Docker GPU 접근이 구성되지 않았다면 설치부터 진행하지 말고 해당 결과를 먼저 확인한다. 외부 컨테이너가 GPU 접근을 예약했다면 현재 GPU 사용률과 별개로 대기할 수 있다.

## 3. simsys4090: 실행 이미지·worker 설정 전달

이미 검증된 amd64 학습 이미지와 이번 Dispatcher 이미지를 전송한다. 새 학습 이미지 재빌드는 필요 없다. 원격에 대용량 학습 이미지와 모델 입력·출력 파일을 둘 공간이 필요하다.

```bash
(
  set -euo pipefail
  cd ~/workspace/agent_project/model_platform
  docker image inspect model-platform-training-axolotl:launch-v1 \
    --format 'arch={{.Architecture}} gate={{index .Config.Labels "model-platform.execution-gate"}}'
  docker image save model-platform-dispatcher:dev model-platform-training-axolotl:launch-v1 |
    ssh simsys@192.168.0.173 docker image load
  scp remote/worker.compose.yml remote/worker.env.example \
    simsys@192.168.0.173:workspace/model_platform_worker/
)
```

simsys4070에서 `worker.env`를 만들고 `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`를 현재 모델 플랫폼의 MinIO 자격 정보로 채운다. 파일 내용을 로그나 대화에 붙이지 않는다. 예시 파일에는 실제 자격 정보가 없다.

```bash
ssh simsys@192.168.0.173
cd ~/workspace/model_platform_worker
cp -n worker.env.example worker.env
chmod 600 worker.env
nano worker.env

docker compose -f worker.compose.yml up -d --no-build
docker compose -f worker.compose.yml ps

docker inspect model-node-agent --format \
  'state={{.State.Status}} network={{.HostConfig.NetworkMode}} devices={{json .HostConfig.DeviceRequests}}'
exit
```

기대값은 `running`, `host`, `devices=null`이다. worker 자체는 GPU를 요청하지 않는다. GPU는 실제 학습 Job이 실행될 때만 할당한다. 원격 노드의 `model-gpu-admission` 볼륨은 해당 호스트의 다른 플랫폼 GPU 생성자와 공유하며 호스트 간에 공유하지 않는다.

## 4. simsys4090: SSH 키·노드 등록 파일

비대화식 연결용 키는 중앙 서버에만 보관한다. 아래 `test`는 기존 키를 덮어쓰지 않도록 한다. `ssh-copy-id`는 지정한 원격 계정에 공개키를 등록한다.

```bash
cd ~/workspace/agent_project/model_platform
mkdir -p remote/ssh
chmod 700 remote/ssh
cp -n remote/ssh-config.example remote/ssh/config
cp -n remote/execution-nodes.example.json remote/execution-nodes.json

test -f remote/ssh/id_ed25519 ||
  ssh-keygen -t ed25519 -N '' -f remote/ssh/id_ed25519 -C model-platform-node
ssh-copy-id -i remote/ssh/id_ed25519.pub simsys@192.168.0.173
```

호스트 키는 이미 신뢰를 확인한 SSH 연결의 `known_hosts`에서 준비한다. 예를 들어 중앙 로그인 계정이 바로 위 SSH 연결에서 서버 키를 확인했다면 다음을 사용한다.

```bash
ssh-keygen -F 192.168.0.173 -f ~/.ssh/known_hosts > remote/ssh/known_hosts
ssh-keygen -lf remote/ssh/known_hosts
chmod 600 remote/ssh/config remote/ssh/id_ed25519 remote/ssh/known_hosts
```

서버 재설치 등으로 키가 바뀌었다면 해당 서버에서 `/etc/ssh/ssh_host_ed25519_key.pub`의 fingerprint를 확인해 대조한다. 빈 `known_hosts`나 확인하지 않은 호스트 키를 허용하는 설정으로 실행하지 않는다. SSH 계정과 키는 Docker 실행 권한을 가진 신뢰된 운영자용이다.

## 5. simsys4090: 터널·원격 probe

터널은 원격 호스트의 **127.0.0.1:19001–19005**에서만 수신한다. Operation, Management, Training, MinIO, MLflow를 각각 연결한다. 원격 SSH 서버는 원격 포트 전달을 허용하고 `GatewayPorts no` 또는 `clientspecified`로 loopback 바인딩을 유지해야 한다. 같은 포트를 쓰는 다른 프로세스가 있으면 터널이 실패하며 임의로 종료하지 않는다.

[OpenSSH의 원격 포트 전달](https://man.openbsd.org/ssh.1)과 [ExitOnForwardFailure·호스트 키 검사](https://man.openbsd.org/ssh_config.5)를 사용한다. 학습 Job은 Linux의 [Docker host 네트워크](https://docs.docker.com/engine/network/drivers/host/)로 해당 loopback에 접속한다. 중앙 gRPC나 Docker TCP 포트를 외부에 공개하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose -f docker-compose.yml -f remote/control.compose.yml config --quiet
  docker compose -f docker-compose.yml -f remote/control.compose.yml stop dispatcher
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    up -d --no-build --no-deps --force-recreate training deployment reconciler node-tunnel
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    up -d --no-build --no-deps --force-recreate dispatcher
  docker compose -f docker-compose.yml -f remote/control.compose.yml ps
  docker compose -f docker-compose.yml -f remote/control.compose.yml logs --tail=30 node-tunnel
  mkdir -p tests/results/nodes
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    exec -T dispatcher /app/dispatcher --probe-node simsys4070 |
    tee tests/results/nodes/simsys4070-probe.json
)
```

probe는 GPU Job을 생성하지 않는다. 노드명, 실제 Docker daemon ID·아키텍처, 로컬 학습 이미지의 실행 gate, Operation RPC 응답, 제어 포트 연결, MinIO·MLflow HTTP health를 검사한다. `status=ready`가 필요하다. 제어 포트 연결만으로 Training/Management의 전체 기능이나 MinIO 쓰기 권한까지 검증되는 것은 아니므로 실제 smoke를 이어서 수행한다.

초기 등록은 `enabled=false`, `daemon_id` 빈 값이다. 성공한 probe로 ID를 고정하고 활성화한다. 기존 노드를 다른 daemon ID로 덮어 바인딩하는 작업은 도구가 거절한다.

```bash
(
  set -euo pipefail
  python tests/enable_remote_node.py --probe tests/results/nodes/simsys4070-probe.json
  docker compose -f docker-compose.yml -f remote/control.compose.yml stop dispatcher
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    up -d --no-build --no-deps --force-recreate training reconciler
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    up -d --no-build --no-deps --force-recreate dispatcher
  docker compose -f docker-compose.yml -f remote/control.compose.yml \
    exec -T dispatcher /app/dispatcher --probe-node simsys4070
)
```

설정 파일은 원자적으로 교체되므로 bind mount가 새 파일을 읽도록 컨테이너를 재생성한다. 활성화 후에는 관련 서비스의 Compose 명령에 **두 `-f`를 계속 사용**한다. 중앙 로컬 노드명 기본값은 `simsys4090`이다. 원격 노드명을 지우거나 다른 물리 호스트로 재사용하면 기존 Operation의 관찰·복구가 깨지므로 안정적으로 유지한다.

## 6. 실제 원격 GPU 학습

probe가 성공하고 원격 GPU 0에서 학습할 수 있을 때 실행한다. 이 명령은 simsys4090 GPU 0을 지정하는 것이 아니라 `simsys4070`의 GPU 0을 지정한다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_dataset_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --node-name simsys4070 \
  --gpu-id 0 \
  --timeout 600
```

검증 순서는 데이터셋 버전 등록·고정 → 원격 Job → 학습 성공 → ready adapter 등록 → 실제 원격 컨테이너 확인이다. 보고서의 `remote_evidence`에 노드·daemon ID·컨테이너·이미지·GPU가 기록된다. 다른 노드의 성공 응답이나 원격 확인 실패를 PASS로 처리하지 않는다.

600초는 각 관찰 단계의 대기 한도다. 최초 모델 다운로드·학습 준비가 느리면 초과할 수 있으며, 시간 초과는 Job을 취소하지 않는다. 새 요청을 만들지 말고 출력된 보고서를 그대로 재개한다.

```bash
python tests/live_dataset_smoke.py \
  --resume tests/results/datasets/실제-보고서-ID.json \
  --timeout 600
```

완료한 adapter는 기존 **중앙 로컬 vLLM 서빙 경로**로 추론 검증할 수 있다. `live_serving_smoke.py --model-version-id`에 이번 출력 adapter ID를 지정한다. 원격 노드에서 장기 실행하는 vLLM은 아직 구현하지 않았다.

## 오류와 보존

| 관찰 | 처리 |
|---|---|
| 잘못되거나 비활성화된 노드의 새 학습 | API에서 거절하며 새 Training/Operation을 만들지 않음 |
| 요청 후 노드가 비활성화되어 Dispatcher가 거절 | `NODE_PLACEMENT_REJECTED`, 확정된 실패 보고 후 Kafka 확인 |
| 원격 GPU 예약 충돌 | 기존 `waiting_resources`와 20초 뒤 재전달, 실제 실행 실패 예산에서 제외 |
| SSH 응답 유실·Docker 생성 불확실 | 실행 권한과 원격 GPU claim을 보존, 다른 노드로 이동하지 않음 |
| 실행 권한을 받은 Job의 통신 중단 | 자동 재시작하지 않으며 원격 증거와 도메인 결과로 관찰 |
| 시작 전 연결 실패 반복 | 기존 최대 3회 시작 복구 예산 적용, 초과 시 수동 확인 |

SSH 실패 때는 중앙 `node-tunnel` 로그, 대상의 `model-node-agent` 상태와 Docker 접근 권한을 확인한다. 상세 SSH 진단은 운영자 터미널에서 실행하고 키·환경 전체를 로그로 공유하지 않는다. 원격 Job은 `model-platform.operation_id` 라벨로 찾을 수 있다.

```bash
# simsys4070에서 실제 Operation UUID를 대입한다.
docker ps -a --filter label=model-platform.operation_id=실제-Operation-UUID
```

원격 Job의 컨테이너와 Docker 로그는 원격 호스트에 남는다. 임시 학습 폴더는 Job이 정상적인 종료 경로를 거치면 정리되며 강제 종료 등으로 남을 수 있다. 현재 중앙 GC는 원격 컨테이너를 자동 수집·삭제하지 않는다. 원격 GPU claim을 삭제하거나 Job을 강제 재시작하지 않는다. 기존 `--recover-gpu-wait` CLI는 중앙 로컬 호스트용이며 원격 요청에 사용하지 않는다. 원격 로그 보존·수집 자동화는 별도 확장 항목이다.
