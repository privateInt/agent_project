# 로컬 인프라와 서비스 실행

모든 명령은 사용자 서버에서 실행한다. 호스트에 Docker/Compose가 필요하고, API 예제에는 curl·jq, smoke에는 Python 3가 필요하다. GPU 학습에는 NVIDIA Container Toolkit과 사용 가능한 GPU가 필요하다.

## 기존 환경 설정

`src/platform/model`과 `src/infra`의 기존 환경 파일·인증 자산을 사용한다. 새 샘플 파일로 덮어쓰지 않는다. 공통 설정의 의미는 아래와 같다.

| 설정 | 용도 / 기본 연결 |
|---|---|
| MODEL_POSTGRES_URL | DB bootstrap URL. 예: `postgresql://USER:PASSWORD@postgres:5432/postgres?sslmode=disable` |
| MODEL_KAFKA_BROKERS | Docker 내부 `ops-kafka:19092` |
| MODEL_MINIO_ENDPOINT / BUCKET | 기본 `ops-minio1:9000` / `model-artifacts` |
| MODEL_MINIO_ACCESS_KEY / SECRET_KEY | 기존 MinIO 자격 증명 |
| MODEL_CREDENTIAL_ENCRYPTION_KEY | 기존 32-byte base64 암호화 키 |
| MODEL_GATEWAY_PORT | 기본 13000 |
| MODEL_DISPATCHER_NODE_NAME | resources.node_name 검증 기준 |
| MODEL_MLFLOW_TRACKING_URI | 컨테이너 내부 기본 `http://ops-mlflow:5000` |
| MODEL_MLFLOW_PUBLIC_URI | 결과 링크용 기본 `http://127.0.0.1:5000` |
| AGENT_PROJECT_NETWORK | 기본 `ops-infra` |
| MODEL_VLLM_IMAGE | 기본 `vllm/vllm-openai:v0.15.0`. 서빙 전에 명시적 pull 필요 |
| MODEL_SERVING_CACHE_VOLUME | 기본 `model-serving-cache`. Deployment와 vLLM이 공유하는 named volume |

[platformdb](../../src/platform/model/internal/platformdb/postgres.go)는 각 서비스 DB명을 사용해 연결하고 최초 실행 시 없으면 생성한다. 최초 생성 role에는 CREATEDB 권한이 필요하다. 스키마 초기화는 각 서비스 시작 시 수행한다. 기존 암호화 키를 교체하면 저장한 임시 credential을 읽지 못할 수 있으므로 이유 없이 재생성하지 않는다.

## 공통 인프라

[infra Compose](../../src/infra/docker-compose.yml)는 PostgreSQL, MinIO, Redis, Kafka KRaft, OpenSearch, MLflow와 n8n/전용 DB를 실행한다. 최초 준비와 A07 n8n 이전은 아래 기동기를 사용한다. [n8n 보존·진단 안내](../agent-platform/n8n-infra-20260924.md)를 따른다.

```bash
cd ~/workspace/agent_project
python3 src/infra/ops/up.py
cd infra
docker compose ps
```

Kafka는 Docker 내부 `ops-kafka:19092`, 호스트 외부 advertised host의 9092, controller 29093을 구분한다. 모델 서비스는 내부 listener를 사용한다. 실제 포트 재정의는 각 infra Compose 설정이 기준이다.

## 이미지 빌드

상시 서비스는 서비스별 Compose의 build target, Job은 별도 Bake 그래프로 빌드한다. 전체 빌드와 개별 수정 명령은 [빌드 안내](build.md)를 따른다. 이번 구조 전환의 테스트·재기동 순서는 [전환 기록](../history/model-platform-202609.md#2026-09-08-03-build-layout)에 있다.

## 실행과 확인

```bash
cd ~/workspace/agent_project/model_platform
docker compose up -d --no-build
docker compose ps
docker compose logs --tail=50 operation management training deployment dispatcher reconciler
curl -fsS http://127.0.0.1:13000/health | jq .
```

예상 시작 로그는 Operation의 gRPC listener·Outbox publisher, Dispatcher의 topic/group, Reconciler의 readiness다. 이후 [제어 smoke](../testing/request-creation.md#control-smoke)로 실제 요청 전달을 확인할 수 있다. 기존 서비스 운영 중 protocol 변경을 배포할 때는 Job과 제어 서비스의 호환 순서를 별도로 검토한다.

실제 서빙은 [서빙 절차](../testing/serving.md)로 생성·검증한다. `model-serving-*` 컨테이너는 동적으로 생성되므로 Compose stop/down으로 멈추지 않는다. 플랫폼 종료 전 배포별 Stop API를 호출하고 status=stopped를 확인한 뒤 `docker compose stop`으로 제어 서비스를 멈춘다. 중지된 서빙 컨테이너·로그·캐시는 유지한다. DB 초기화나 볼륨 삭제는 필요 없다.
