# 운영 팁과 오류 확인

## tar 수정 적용

전체 보관본과 변경 적용용 파일은 구분한다. 전체 보관본은 빈 디렉터리에서 확인한다. 기존 서버의 파일을 갱신할 때는 해당 적용 파일의 대상 경로·내용 확인 절차를 따른다.

`tar -xvf`는 같은 경로를 교체하지만 삭제된 구문서를 제거하지 않는다. 이동·삭제가 있으면 정확한 목록과 내용 해시를 확인하는 외부 적용 도구로 반영한다. 사용자 변경과 새 파일은 보존하고 일회성 도구를 프로젝트 안에 설치하지 않는다. 문서만 변경한 적용에는 Docker 재빌드·재시작이 필요 없다.

## curl URL과 식별자

`curl: (3) URL using bad/illegal format or missing URL`이면 현재 셸의 주소와 ID부터 확인한다. 새 셸·서브셸에서는 이전 변수가 남아 있지 않을 수 있다.

```bash
API_BASE='http://127.0.0.1:13000'
MODEL_VERSION_ID='실제-ready-snapshot-UUID'
printf 'API_BASE=<%s>\nMODEL_VERSION_ID=<%s>\n' "$API_BASE" "$MODEL_VERSION_ID"
curl -fsS "$API_BASE/api/model-versions/$MODEL_VERSION_ID" |
  jq '{model_version_id,state,artifact_kind,repo_id}'
```

ID는 현재 DB에서 조회한 실제 ready snapshot 값으로 바꾼다. 과거 초기화 전 UUID를 그대로 사용하지 않는다. 생성 요청 패치의 전체 서비스 전환 후에는 POST 전에 키를 보관하고, 응답 유실 시 같은 Idempotency-Key·같은 입력으로 재전송한다. 키를 생략한 요청은 중복 보호가 없으므로 조회·로그를 먼저 확인한다. 202 뒤 Operation이 아직 404이면 [도메인 전달 상태](../reference/request-creation.md)를 먼저 확인한다.

## Management 재시작과 legacy artifact

`unsupported model_versions.artifact_kind`는 서버가 DB에 연결된 뒤 기존 메타데이터와 허용값을 검사하다 실패한 경우다. 현재 소스는 legacy `file`을 보존한다. 오류가 재발하면 실행 중인 Management 이미지와 현재 소스부터 맞춘다. 19-byte 테스트 `.bin` 파일을 `snapshot`으로 바꾸어 학습 입력처럼 취급하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose logs --tail=50 management

docker exec -i postgres sh -c \
  'psql -X -U "$POSTGRES_USER" -d model_management -v ON_ERROR_STOP=1' <<'SQL'
SELECT model_version_id, state, source_type, artifact_kind, artifact_size_bytes
FROM model_versions
ORDER BY created_at DESC LIMIT 20;
SQL
```

## Operation과 Outbox 진단

```bash
cd ~/workspace/agent_project/model_platform
docker compose logs --tail=80 operation dispatcher reconciler

docker exec -i postgres sh -c \
  'psql -X -U "$POSTGRES_USER" -d model_operations -v ON_ERROR_STOP=1' <<'SQL'
SELECT count(*) FILTER (WHERE published_at IS NULL) AS pending_events,
       count(*) FILTER (WHERE published_at IS NOT NULL) AS published_events
FROM operation_outbox;

SELECT operation_id, status, phase, dispatch_generation,
       dispatch_lease_until, execution_started_at,
       reconcile_checked_at, reconcile_note
FROM operations
WHERE status IN ('pending', 'dispatching', 'running')
ORDER BY updated_at DESC LIMIT 20;
SQL
```

Outbox 0/0은 아직 이벤트가 없다는 뜻이다. 발행·소비 성공 근거로 삼지 않는다. `reconcile_checked_at` 행이 없으면 관찰 대상이 아직 없거나 나이 조건을 충족하지 않았을 수 있다. `dispatching` 중복은 [실행 프로토콜](../reference/execution-protocol.md)의 만료·시작 승인 여부를 함께 본다. 관찰 없이 수동으로 status/token을 일괄 변경하지 않는다.

PostgreSQL `42P08 inconsistent types deduced`가 재발하면 같은 매개변수를 UPDATE와 CASE에서 사용하는 SQL의 명시적 `::varchar`를 확인한다. lease 판정은 row lock 이후 `clock_timestamp()`를 유지해야 한다.

## Kafka와 Job 로그

```bash
docker exec ops-kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:19092 --describe --group model-platform-dispatchers

docker ps -a --filter 'name=model-op-'
```

consumer group을 재정의한 환경에서는 실제 group 이름으로 바꾼다. 출력된 정확한 Job 컨테이너 이름으로 `docker logs <container-name>`을 확인한다. 완료 컨테이너 정리는 [GC 모드](gc.md)에 따른다. 현재 Compose 기본값은 apply·성공 보존 1h·실패 보존 168h다. 실행 파일 단독 사용의 fallback은 dry-run이므로 실제 적용 모드는 환경/Compose 설정에서 확인한다. archive/apply에서 로그 저장·검증을 수행한다. 전체 Job을 일괄 삭제해 보존 정책을 우회하지 않는다.

## GPU·MLflow

호스트 GPU 1을 선택해도 컨테이너 안에서는 `cuda:0`으로 보일 수 있다. `resources.gpu_ids`와 Docker 요청값을 함께 확인한다. MLflow의 내부 tracking URI와 브라우저 public URI를 구분하며, Training 응답의 `mlflow_run_url`과 `.result`를 조회한다. 실제 학습 테스트 절차는 [모델 작업 테스트](../testing/model-workflow.md)에 있다.

## CPU demo와 실제 추론

CPU demo ready는 모델 로드나 추론을 의미하지 않는다. 실제 생성 요청은 `serving.engine=vllm`을 명시하고 [서빙 smoke](../testing/serving.md)로 채팅 응답까지 확인한다. 상태·오류·중지는 [서빙 계약](../reference/serving.md)이 기준이다. endpoint의 127.0.0.1은 Docker 호스트를 가리키며, 같은 네트워크의 다른 컨테이너는 internal_endpoint를 사용한다. 플랫폼의 학습·서빙 생성 경합은 [로컬 GPU 점유 관리](../reference/multi-gpu.md)가 제어한다. 호스트에서 직접 실행한 CUDA 작업과 다른 daemon의 작업은 별도 확인한다.

학습 adapter는 [전용 검증·진단](../testing/adapter-serving.md)을 따른다. `ADAPTER_PREPARATION_FAILED`는 message에서 manifest·tokenizer·embedding 검사 실패를 확인한다. 저장된 embedding이 base와 다른 경우 원본 tensor를 지우거나 adapter를 snapshot으로 재표기해 우회하지 않는다. GPU 준비가 성공해야 실제 채팅 검증으로 진행한다.

## external-gpu

플랫폼의 GPU admission은 다른 컨테이너가 GPU 접근 권한을 갖고 실행 중이면 보수적으로 점유자로 볼 수 있다. 순간 `nvidia-smi` 사용률이 0이어도 이후 GPU 사용 가능성이 없다는 뜻은 아니다. 외부 서비스의 Docker DeviceRequests와 Compose GPU 예약·노출 환경을 함께 확인한다.

2026-09-09 ContextRAG의 GPU 접근 설정을 별도로 해제한 뒤 플랫폼의 대기 중 학습·새 adapter 서빙이 통과했다. 이는 해당 backend 전체의 CPU 호환성 검증이 아니다. 당시 외부 프로젝트의 원본 Compose와 일회성 override는 현재 첨부에 없으므로 예전 복사·재생성 명령을 재사용하지 않는다. 외부 서비스 설정을 바꿔야 할 때는 그 프로젝트의 현재 설정과 기능 요구를 먼저 확인한다.
