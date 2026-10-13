# 생성 요청·전달 복구 검증

## 격리된 REST·gRPC·PostgreSQL 테스트

호스트의 실제 DB 연결을 사용하되 테스트마다 고유 schema를 만들고 종료 시 제거한다. REST는 Fiber 테스트 서버, gRPC는 테스트 컨테이너의 임시 loopback 포트다. Operation publisher를 실행하지 않아 실제 Kafka 메시지·Job·GPU 작업은 생성하지 않는다. 장애는 이 테스트 서버의 interceptor와 격리 테이블 제약으로 주입한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker build --target builder -t model-platform-check:idempotency .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra     -e OPERATION_TEST_POSTGRES_URL     model-platform-check:idempotency     go test -count=1 -v       ./internal/idempotency ./internal/submission ./tests/creation       ./operation/app ./operation/outbox ./internal/operationreport
)
```

`OPERATION_TEST_POSTGRES_URL` 미설정 시 DB 테스트가 SKIP되므로 실제 실행 여부를 확인한다. DB role에는 연결 DB 안의 schema 생성·삭제 권한이 필요하다. 테스트가 강제 종료되면 해당 실행의 `request_test_...` schema가 남을 수 있다. 다른 schema를 일괄 삭제하지 않는다.

| 테스트 영역 | 확인할 실패 경계 |
|---|---|
| 4개 생성 API | 같은 키·기본값 정규화 재현, 다른 입력 409, 다른 API 범위 |
| 동시 REST 요청 | domain 1개, Operation 1개, Outbox 1개 |
| 키 입력 | 생략 시 새 요청, 빈 헤더·중복·형식·길이 오류 400 |
| Operation 생성 | 같은 ID 동시 호출, UUID·GPU 정규화, terminal 재현, 입력 충돌 |
| 도메인 트랜잭션 | 전달 행 삽입 실패 시 domain·credential·키 예약 전부 rollback |
| 원격 장애 | 202 수락, pending 조회, source 조회 장애 중에도 재현 |
| 재기동 복구 | 새 worker 인스턴스가 DB만으로 미전달 요청 복구 |
| 응답·ACK 유실 | 원격 커밋 후 재전달 시 새 이벤트 없음; credential 유지 |
| 큐 경합·호환성 | 잠긴 항목 건너뜀, 영구 오류 blocked, Unimplemented 대기·fallback 없음 |
| credential 만료 | 재전송이 TTL을 늘리지 않으며 만료 후 익명 요청으로 전환하지 않음 |

worker 재기동 테스트는 새로운 worker 인스턴스로 검증한다. 실제 호스트 서비스 SIGKILL·네트워크 단절이나 GPU import를 이 단계에서 수행하지 않는다. 기존 Dispatcher SIGKILL 검증과는 범위가 다르다.

## 이미지 전환

위 테스트가 모두 PASS한 뒤 실행한다. 공유 Go 소스와 추가 protobuf 계약은 다섯 이미지에 포함되어야 한다. 서비스별 Compose·Dockerfile/Bake 구성을 그대로 사용한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build operation management training deployment gateway
  docker compose up -d --no-deps --force-recreate operation
  docker compose up -d --no-deps --force-recreate management training deployment
  docker compose up -d --no-deps --force-recreate gateway
  docker compose ps operation management training deployment gateway
  docker compose logs --tail=50 operation management training deployment gateway
)
```

Operation의 gRPC·Outbox 기동과 세 도메인의 `operation submission worker started`를 확인한다. Gateway는 마지막에 전환하며 그 전에는 키를 사용하는 새 클라이언트 트래픽을 시작하지 않는다. 실행 중인 Job·Dispatcher·Reconciler·GC와 상시 GC 모드는 이번 전환 대상이 아니다.

`up -d` 반환은 각 서비스의 준비 완료 보장이 아니므로 로그에 startup 오류·재시작이 없는지 확인한 뒤 아래 smoke를 실행한다. schema는 각 서비스 초기화 시 추가된다. 수동 SQL migration이나 환경 키 교체는 필요 없다.

## 실제 제어 경로 smoke

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_control_smoke.py
```

기존 ready snapshot을 사용하는 CPU demo 배포이며 실제 Kafka·Dispatcher·실행 승인·Reconciler를 확인한다. 이번에는 domain GET의 `submission_status=delivered`를 먼저 기다려 등록 전 Operation 404를 작업 실패로 취급하지 않는다. 결과 확인·정리 범위는 [제어 경로 smoke](request-creation.md#control-smoke)를 따른다.

헤더 재현 자체는 위 REST 통합 테스트가 확인한다. 이 smoke는 기존 헤더 생략 클라이언트의 호환성을 검증하며 POST를 자동 재전송하지 않는다.

## 전달 큐 조회

세 도메인 DB에서 상태를 확인한다. `api_requests.response`, 큐의 request 원문, credential 값은 조회할 필요가 없다.

```bash
cd ~/workspace/agent_project/model_platform
for REQUEST_DB in model_management model_training model_deployment; do
  docker exec -i -e REQUEST_DB="$REQUEST_DB" postgres sh -c     'exec psql -X -U "$POSTGRES_USER" -d "$REQUEST_DB" -v ON_ERROR_STOP=1' <<'SQL'
SELECT current_database() AS domain_db, status, count(*)
FROM operation_submissions GROUP BY status ORDER BY status;
SELECT operation_id, resource_type, resource_id, status, attempts,
       last_error_code, next_attempt_at, delivered_at
FROM operation_submissions WHERE status <> 'delivered'
ORDER BY created_at LIMIT 20;
SQL
done
```

새 요청이 없는 DB의 0행은 정상이다. pending이 쌓이면 도메인·Operation 로그와 연결을 확인하고, `Unimplemented`면 Operation 이미지 버전을 맞춘다. blocked에는 자동 재시도가 없으므로 [오류 분류](../reference/request-creation.md)를 따른다. 기존 Operation·도메인 상태를 일괄 수정하거나 다른 키로 같은 작업을 새로 만드는 방식으로 해소하지 않는다.


<a id="control-smoke"></a>

## CPU 제어 smoke

[로컬 운영](../operations/local.md)에 따라 실행 승인 label이 있는 Job 이미지와 control 서비스를 기동한 환경에서 실행한다. Python 표준 라이브러리, Docker CLI, 기존 postgres 컨테이너의 psql을 사용한다. 상위 Compose에서 Deployment Job 태그를 읽고, CPU sleep fixture는 실행 중인 Operation 이미지 ID를 사용한다. 이전 통합 control 이미지 태그는 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_control_smoke.py
```

다른 모델 또는 API 주소를 사용할 때:

```bash
cd ~/workspace/agent_project/model_platform
python tests/live_control_smoke.py \
  --api http://127.0.0.1:13000 \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885 \
  --timeout 300
```

모델은 실제 DB의 ready snapshot이어야 한다. 기본 ID는 위 개발 서버 snapshot이다.

| 단계 | 확인 대상 |
|---|---|
| 1 | CPU demo Deployment를 REST로 요청하고 Outbox → 실제 Kafka → Dispatcher → Job 성공 확인. DB의 protocol 1, execution_started_at, process UUID 검사 |
| 2 | 방금 완료한 demo를 참조하는 legacy protocol 0 합성 Operation의 도메인 완료 보정 |
| 3 | 소유 CPU sleep 컨테이너와 legacy protocol 0 합성 Operation의 running 보정 |

protocol 1의 실행 승인을 Docker running 관찰로 대체할 수 없다. 이 테스트의 2·3단계는 의도적으로 legacy fixture를 사용한다. GPU 학습, vLLM, 실제 Dispatcher 중단은 이 테스트 범위 밖이며 장애 주입은 [별도 테스트](request-creation.md#dispatch-fault)에서 수행한다.

### 남는 자료와 정리

이번 실행의 합성 Operation을 cancelled로 바꾸고 소유 smoke label을 확인한 sleep 컨테이너를 제거한다. 실제 demo Deployment·Operation·Outbox와 완료 demo Job 컨테이너는 진단 자료로 남는다. 모델 파일은 만들지 않는다. 이 smoke는 Idempotency-Key를 생략하는 호환성 클라이언트이므로 POST를 자동 재전송하지 않는다. 202 뒤에는 domain의 전달 ACK를 먼저 기다리고 Operation 완료를 조회한다.

최종 PASS와 fixture cleanup 완료를 확인한다. `docker compose ps`에서 Up만 확인하는 것보다 넓은 경로를 검증하지만, 이를 학습·추론 E2E 성공으로 해석하지 않는다. 구현은 [live_control_smoke.py](../../src/platform/model/tests/live_control_smoke.py)에 있다.


<a id="dispatch-fault"></a>

## Dispatcher 프로세스 장애 검증

시작 승인 전 Dispatcher를 중단하고 lease 복구·이벤트 재발행·이전 실행 차단을 확인한다. 정상 서비스와 별도로 실제 control 바이너리를 사용하는 테스트 인스턴스를 띄운다. Docker CLI 실행 직전 지연 도구만 테스트 이미지에 추가한다.

### 준비와 실행

정상 Management와 공통 PostgreSQL/Kafka, ready snapshot, launch-v1 Deployment Job 이미지가 필요하다. [로컬 운영](../operations/local.md)에서 준비한다. 테스트 이미지는 현재 소스의 Go builder와 Docker runtime을 Bake로 직접 연결한다. 이전 통합 control 태그를 다시 빌드할 필요는 없다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  python tests/check_build_layout.py
  python -m unittest discover -s tests -p 'test_live*.py' -v
  docker buildx bake --builder default -f docker-bake.hcl --load check dispatch-fault
  docker run --rm model-platform-check:build-layout \
    go test -count=1 -v ./tests/dispatch_fault
  python tests/live_dispatch_fault.py
)
```

기존 Deployment Job 이미지가 현재 코드와 다르면 먼저 해당 이미지를 빌드한다. 테스트는 Compose가 지정한 Deployment Job 이미지를 사용한다. 보통 실행에 3~5분이 걸리고 빌드 시간은 별도다. 120초 lease를 SQL로 줄이지 않는다.

기본 모델은 `cceae526-7b7c-47d4-870b-117372b51885`다. 다른 DB에서는 `python tests/live_dispatch_fault.py --model-version-id <UUID> --timeout 300`으로 지정한다.

### 확인 순서

| 단계 | 필수 확인 |
|---|---|
| 최초 Claim | generation 1, 아직 execution_started_at 없음 |
| 실제 SIGKILL | Dispatcher 종료 코드 137, OOM 아님, 첫 offset 처리 완료 전 |
| 자연 lease 만료 | Reconciler가 pending/launch_retry_queued로 돌리고 이전 token 제거 |
| Outbox 재발행 | event_id 동일, publish_attempts 증가 |
| Dispatcher 재시작 | generation 2, attempt 1, 새 token, demo ready, Operation succeeded |
| 전달 완료 | 원래·재발행 레코드의 offset 0·1 처리 완료 |
| 지연된 이전 Job | 승리한 Job 완료 후 이전 token으로 시작한 실제 Job 거부, 결과 행 불변 |

CPU demo를 사용한다. GPU 학습, vLLM, 브로커 중단, BeginExecution 승인 이후 재실행은 포함하지 않는다.

### 격리와 정리

매 실행 UUID로 `model_operations`, `model_deployment` 내부에 전용 `dispatch_smoke_<UUID>` schema를 만들고 search_path를 격리한다. Kafka topic·consumer group, 제어 컨테이너·CPU Job도 실행별 이름을 사용한다. 정상 Management에서는 ready 모델 메타데이터만 조회한다.

종료 시 해당 실행의 소유 컨테이너·DB schema·topic·group을 정리한다. group 삭제는 consumer 세션 만료를 기다릴 수 있다. 정리가 실패하면 최종 PASS를 내지 않는다. 강제 종료나 Docker/DB 장애로 남은 리소스는 보고서의 실행 UUID와 cleanup 오류를 기준으로 확인한다.

### 결과 읽기

출력된 `[report]` 경로는 `tests/results/dispatch-fault/<run UUID>/report.json`이다. 성공 시 핵심 필드는 다음과 같다.

```json
{
  "passed": true,
  "crash": {"exit_code": 137, "generation": 1, "execution_started": false},
  "recovery": {"dispatch_generation": 2, "attempt": 1, "status": "succeeded"},
  "handled_offsets": [0, 1],
  "stale_token_rejected": true,
  "cleanup_errors": []
}
```

테스트 Operation은 종료 후 전용 schema와 함께 제거된다. 정상 Gateway의 Operation 조회 대신 이 보고서를 사용한다. 실패하면 같은 폴더의 `fault.log`, `reconciler.log`, `recovery.log`, `stale.log` 등 생성된 로그를 확인한다. [실행기](../../src/platform/model/tests/live_dispatch_fault.py), [Python 모의 테스트](../../src/platform/model/tests/test_live_dispatch_fault.py), [Go helper](../../src/platform/model/tests/dispatch_fault)가 구현 원본이다.
