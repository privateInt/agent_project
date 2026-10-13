# 명시적 원격 노드 실행 계약

`POST /api/training-runs`의 기존 `resources.node_name`을 사용한다. 요청·TrainingRun·Operation·Job이 같은 이름을 유지하며, 이름을 생략하면 중앙의 기존 로컬 Docker 경로를 사용한다. 첫 구현은 Linux amd64 원격 노드의 GPU 하나를 지정하는 학습이다.

```json
{
  "source_model_version_id": "cceae526-7b7c-47d4-870b-117372b51885",
  "dataset": {
    "source_type": "version",
    "dataset_version_id": "등록된-데이터셋-버전-UUID"
  },
  "resources": {"node_name": "simsys4070", "gpu_ids": [0]}
}
```

## 배치와 식별

`EXECUTION_NODES_FILE`은 운영자가 승인한 정적 노드 목록이다. 노드명·SSH alias·worker 컨테이너명·아키텍처·Docker daemon ID·활성화 여부를 저장한다. 첫 probe는 비활성화 상태에서 가능하며, 실제 daemon ID를 고정한 뒤 활성화해야 새 학습을 허용한다. 이름·SSH alias·daemon ID 중복, 셸 구문이 들어간 이름, 지원하지 않는 아키텍처는 거절한다.

Training은 새 요청의 노드 허용 여부를 DB 트랜잭션에서 검사한다. 이미 커밋된 같은 멱등 키의 응답은 노드를 비활성화한 후에도 유지한다. 노드명은 기존 요청 fingerprint의 resources에 포함되므로 같은 키로 다른 노드로 바꾸면 충돌이다. Dispatcher는 Kafka의 배치 필드를 신뢰하지 않고 Claim 응답의 DB 값을 사용한다. 미등록·비활성 노드 또는 원격에서 지원하지 않는 실행 종류는 확정된 시작 전 실패로 보고한다.

## 실행과 통신

중앙 Dispatcher는 SSH의 고정 명령 `docker exec -i model-node-agent /app/dispatcher --node-request`를 호출한다. UUID와 dispatch token은 암호화된 stdin으로 전달한다. 요청은 임의 이미지·명령·환경 변수·마운트를 받지 않는다. 원격 Registry가 해당 호스트의 승인된 이미지와 전달 환경 변수를 선택한다.

worker는 Operation을 다시 읽어 노드명·종류·리소스·현재 token·lease를 검사하고, 자신의 Docker daemon ID와 배치의 고정 ID가 같은지 확인한다. 실제 Job은 기존 BeginExecution을 통과한 후에만 도메인 처리·GPU 학습을 시작한다. SSH는 운반 수단이며 DB 실행 권한 검사를 대체하지 않는다. 같은 grant의 컨테이너가 이미 있으면 다시 생성하지 않는다.

원격 GPU admission은 그 호스트의 Docker inventory와 **그 호스트의** `model-gpu-admission` 볼륨에서 수행한다. 잠금·durable creation intent·알 수 없는 생성 결과 보존은 로컬과 동일하다. 확인된 BusyError만 중앙에 GPU 대기로 전달한다. SSH 응답 유실, 프로토콜/identity 불일치, Docker 타임아웃은 GPU 대기로 오인하지 않는다.

별도 중앙 `node-tunnel` 컨테이너가 내부 서비스 다섯 개를 원격 loopback으로 전달한다. Job은 remote host 네트워크를 쓰며 서비스 gRPC·MinIO·MLflow 데이터가 터널을 통과한다. 키는 중앙에만 있고 MinIO 자격 정보는 원격 worker의 별도 환경 파일에 있다. 앱 내부 gRPC는 현재 trusted-host 모델이며 SSH로 전송 구간을 보호한다.

## 관찰과 보존

Reconciler는 기존처럼 도메인의 확정 성공/실패를 우선한다. 실행 승인 후 미완료 상태를 관찰할 때는 선택한 원격 노드의 실제 컨테이너를 조회한다. admission을 비활성화해도 기존 노드의 관찰은 유지한다. 연결이 끊겼다는 이유로 로컬 컨테이너를 찾거나 다른 호스트에서 재실행하지 않는다.

원격 inspect는 컨테이너 ID·이미지 ID·상태·exit code·정확한 GPU IDs만 사용자 보고서로 돌려준다. 내부적으로 owner·operation·dispatch token·실행 종류·실행 노드 라벨이 일치해야 반환한다. 환경 변수나 전체 Docker inspect는 반환하지 않는다. `live_dataset_smoke.py --node-name`은 API의 배치 고정과 이 실제 원격 증거를 함께 검사한다.

시작 전 lease 복구와 최대 3회 실제 시작 실패 예산은 기존 Operation 프로토콜을 유지한다. GPU 대기는 별도 횟수로 처리한다. 이미 실행 승인된 Job의 자동 재시작, 다른 노드로 이동, 원격 Job 자동 GC는 없다. 모델 결과물은 중앙 MinIO·Management로 등록되며 중앙 vLLM에서 서빙할 수 있다.

[설치·검증 순서](../testing/remote-nodes.md), [실행 프로토콜](execution-protocol.md), [GPU 대기](../testing/gpu-wait.md)를 함께 참고한다.
