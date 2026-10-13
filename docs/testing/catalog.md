# 목록·검색 검증

신규 목록·필터·커서 API, Dataset 검색, Streamlit에서 사용할 요약 응답을 검증한다. 이번 패치는 실행 승인·GPU 실행 명세를 바꾸지 않으며 실제 GPU 학습을 다시 수행할 필요는 없다.

작성 환경에서는 Python **13개(새 catalog smoke 5개·기존 Dataset 8개)**와 소스 정적 검사를 통과했다. 이후 2026-09-09 사용자 서버에서 **Go 75개·하위 사례 73개 PASS, 실패·SKIP 없음**을 확인했다. 추가한 10개 테스트·하위 사례 38개와 실제 PostgreSQL 통합 검증을 포함한다.

**Go/DB·서비스 전환·실제 REST 검증을 모두 완료했다.** 2026-09-09 08:58:27–08:58:28 UTC 서비스 5개가 정상 기동했고, 시각 파싱 수정 후 공유된 실제 REST 재실행도 PASS했다. 현재 추가 빌드나 재실행은 필요 없다. 아래 명령은 이후 관련 코드 변경 시 재검증에 사용한다. 자세한 검증 근거는 [개발 이력](../history/model-platform-202609.md#2026-09-09-09-catalog-api)에 있다.

## 실제 REST 검증 완료 결과

2026-09-10 공유된 실행 로그에서 다음 결과와 최종 PASS를 확인했다. 보고서 경로는 `src/platform/model/tests/results/catalog/d9c2f3ea-5dd9-42de-bb79-e12b909e763b.json`이다.

| 목록 | 첫 페이지 | 다음 페이지 | 실제 페이지 이동 |
|---|---:|---:|---|
| models | 2 | 2 | 확인 |
| model-versions | 2 | 2 | 확인 |
| training-runs | 2 | 2 | 확인 |
| deployments | 2 | 2 | 확인 |
| operations | 2 | 2 | 확인 |

명령에 base snapshot `cceae526-7b7c-47d4-870b-117372b51885`를 지정했으므로 최종 PASS는 해당 버전의 필터 목록·상세 정보와 모델 이름·유형 일치 검사까지 포함한다. 정렬·ID 중복·공개 필드 검사, 잘못된 입력 및 다른 필터의 커서 거절, 모델·Dataset 빈 검색 결과도 통과했다. GET만 사용하며 새 Job이나 GPU 작업을 실행하지 않았다. 모든 필터 조합을 live에서 확인한 것은 아니며 상세 경계 사례는 아래 DB 통합 테스트에서 검증했다.

### 시각 파싱 보완

공통 `tests/smoke_timestamps.py`가 소수 초 생략 또는 1..9자리, `Z`·숫자 시간대를 처리한다. 초 이하 값을 잘라내거나 float로 변환하지 않고 UTC 초·나노초 값으로 비교하므로 정렬과 lease 경계를 유지한다. 같은 시각 파싱을 사용하던 Dataset 대기 관찰·Dispatcher 장애 smoke도 수정했지만, 이 문제를 검증하기 위해 GPU/장애 주입 smoke를 다시 실행할 필요는 없다.

관련 Python 테스트 **36개**가 작성 환경 Python 3.12에서 PASS했다. 실패한 `.92866` 값, 0..9자리 소수 초, 시간대 변환, 나노초 정렬·UUID 순서, 잘못된 날짜·시간대, lease 경계를 확인했다. fromisoformat을 호출하면 실패하도록 한 테스트도 통과했다. 이후 사용자 서버에서도 전체 catalog smoke가 PASS했다. 서버의 정확한 Python 버전은 공유된 출력에 없으며, 새 catalog 보고서의 `python_version`에서 확인할 수 있다.

## 1. Go/DB 테스트 — 완료

시각 파싱 수정까지 적용한 최신 소스를 기준으로 한다. 과거 기능 패치를 다시 덮어쓸 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform

(
  set -euo pipefail
  docker build --target builder -t model-platform-check:catalog .

  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL

  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:catalog \
    go test -count=1 -v \
      ./internal/listing ./management/app ./training/app \
      ./deployment/app ./operation/app ./gateway/app ./tests/creation
)
```

DB 테스트는 고유 schema에 REST·gRPC 서버와 메타데이터 fixture를 구성하고 종료 시 해당 schema를 정리한다. 실제 학습·서빙 Job은 실행하지 않는다. `TestListKeysetAcrossCollections`는 5개 컬렉션의 동일 시각 정렬, 페이지 사이 새 항목 추가, 커서 행 삭제, 정확한 다음 페이지·마지막 페이지를 검사한다. 검색 테스트는 한글·영문 대소문자·`%`/`_`와 SQL처럼 보이는 입력을 확인한다. 관계·상태 필터, 중지된 배포의 endpoint 제거, REST/gRPC 목록의 실행 승인 값 제외도 확인한다.

PostgreSQL 테스트가 SKIP이면 이 단계의 검증 완료로 기록하지 않는다. 이전 생성 요청·Dataset·Operation 테스트를 함께 실행해 새 목록이 기존 기능을 바꾸지 않았는지 확인한다.

## 2. 이미지 전환 — 완료

Management·Training·Deployment·Operation·Gateway **5개**를 교체한다. 새 protobuf RPC는 추가만 했으므로 기존 Dispatcher·Reconciler·Job 이미지와 호환된다. 학습 이미지 재빌드나 원격 노드 설정은 필요 없다.

```bash
cd ~/workspace/agent_project/model_platform

(
  set -euo pipefail
  docker compose build management training deployment operation gateway

  docker compose up -d --no-build --no-deps --force-recreate \
    operation management training deployment
  docker compose up -d --no-build --no-deps --force-recreate gateway

  docker compose ps management training deployment operation gateway
  docker compose logs --tail=40 management training deployment operation gateway
)
```

각 서비스가 정상 기동한 뒤 다음 단계를 실행한다. 스키마 초기화는 조회 인덱스 5개를 추가하며 기존 데이터·상태는 변경하지 않는다. 과거 모델 메타데이터 정규화 등 기존 스키마 초기화 동작은 그대로 유지한다.

## 3. 실제 REST 조회 smoke — 완료

```bash
cd ~/workspace/agent_project/model_platform

python tests/live_catalog_smoke.py \
  --model-version-id cceae526-7b7c-47d4-870b-117372b51885
```

이 도구는 **GET만 사용**한다. 모델·Dataset·학습·배포를 생성하지 않으며 GPU·Docker 명령도 사용하지 않는다. 위 ID는 앞서 검증한 base snapshot이다. `--model-version-id`를 생략해도 목록을 검증하지만 특정 버전의 목록·상세·모델 이름 일치 검증은 수행하지 않는다.

각 목록에서 최대 두 페이지의 형식·순서·ID 중복, 필터 변경 커서 거절, 잘못된 입력 400, 빈 검색 결과와 공개 필드를 검사한다. 데이터가 적어 다음 페이지가 없으면 해당 컬렉션은 `pagination_observed=false`로 기록한다. 이 경우 페이지 경계의 근거는 1단계 DB 통합 테스트이며 live에서 확인했다고 표시하지 않는다. 실행 중 상태 변경에 따라 조회 결과는 달라질 수 있다.

보고서는 `src/platform/model/tests/results/catalog/<UUID>.json`에 저장한다. `passed=true`와 선택한 버전의 `target_version_verified=true`를 확인한다. 실패 시 같은 명령을 재실행해도 생성되는 플랫폼 리소스는 없다.

## 직접 검색 예시

```bash
curl -fsS -G 'http://127.0.0.1:13000/api/models' \
  --data-urlencode 'q=gemma' --data-urlencode 'model_type=llm' | jq

curl -fsS -G 'http://127.0.0.1:13000/api/model-versions' \
  --data-urlencode 'q=gemma' --data-urlencode 'state=ready' \
  --data-urlencode 'artifact_kind=adapter' | jq

curl -fsS -G 'http://127.0.0.1:13000/api/training-runs' \
  --data-urlencode 'source_model_version_id=cceae526-7b7c-47d4-870b-117372b51885' \
  --data-urlencode 'status=succeeded' | jq

curl -fsS -G 'http://127.0.0.1:13000/api/deployments' \
  --data-urlencode 'status=stopped' | jq

curl -fsS -G 'http://127.0.0.1:13000/api/operations' \
  --data-urlencode 'phase=waiting_resources' | jq
```

상세 필드·필터·페이지 이동 규칙은 [API 계약](../reference/catalog.md)에 있다.
