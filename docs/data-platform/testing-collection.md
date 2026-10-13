# 단일 URL 수집 적용·검증

이번 패키지는 `agent_project/data_platform`과 `agent_project/docs/data-platform`에 새 파일을 추가한다. `model_platform`을 중지하거나 이미지를 교체할 필요가 없다. 기존 PostgreSQL·Kafka·MinIO·ops-infra 네트워크는 정상 실행 중이어야 한다.

## 1. 적용과 접속 설정

현재 프로젝트 소스에는 관련 수정이 포함되어 있다. 필요한 코드 변경 뒤 아래 검사·빌드 절차만 실행한다.

파일을 `~/workspace/agent_project`에 업로드한 뒤 실행한다. venv와 추가 pip 설치는 필요하지 않다. Python 도구는 기존 Python 3.10에서 표준 라이브러리만 사용한다.

```bash
cd ~/workspace/agent_project/data_platform

python ops/configure.py --model-platform ../model_platform
python tests/test_collection_tools.py -v
```

configure는 Model Compose의 렌더링 결과에서 PostgreSQL·Kafka·MinIO·네트워크 접속값만 읽는다. Data 설정을 0600 권한으로 만들고 Data Compose 해석 결과와 실제 값이 같은지도 확인한다. 접속 비밀번호나 HF 토큰을 출력하지 않는다. `.env`가 이미 있으면 덮어쓰지 않으므로 재검증할 때는 configure를 반복할 필요가 없다. 기본 Gateway 주소는 `http://127.0.0.1:14000`이다.

사용 중인 Compose는 기존 모델 플랫폼에서 include를 사용하던 버전이면 된다. 현재 Docker daemon과 통신하는 CLI를 Dispatcher·Reconciler 이미지에 포함한다. 모델의 Docker API 버전 설정을 별도로 복사하지 않는다.

설정에서 `$`가 다시 치환되지 않도록 작은따옴표를 사용하고, 생성 후 실제 해석 값을 비교한다. [Docker Compose 환경 파일 문법](https://docs.docker.com/compose/how-tos/environment-variables/variable-interpolation/#env-file-syntax)

## 2. Go·실제 DB 테스트

모듈·protobuf·Go는 builder 이미지 안에서 준비한다. 호스트에 Go를 설치하지 않는다. 테스트는 임의 이름의 별도 schema를 만들고 끝나면 제거하며, 모델·데이터 플랫폼의 실제 업무 테이블을 초기화하지 않는다.

```bash
cd ~/workspace/agent_project/data_platform
(
  set -euo pipefail

  docker build --target builder -t data-platform-check:collection .

  DATA_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services["data-operation"].environment.POSTGRES_URL'
  )
  DATA_TEST_NETWORK=$(
    docker compose config --format json |
      jq -er '.networks["data-infra"].name'
  )
  export DATA_TEST_POSTGRES_URL

  mkdir -p tests/results/collection
  docker run --rm --network "$DATA_TEST_NETWORK" \
    -e DATA_TEST_POSTGRES_URL \
    data-platform-check:collection \
    go test -count=1 -v ./... \
    2>&1 | tee tests/results/collection/go-tests.log

  if grep -q -- '--- SKIP:' tests/results/collection/go-tests.log; then
    echo 'Go tests skipped; check DATA_TEST_POSTGRES_URL before continuing.'
    exit 1
  fi
)
```

테스트에는 실제 DB의 요청 동시성·전달 기록 원자성·결과 시작 승인·해시 검증·완료 재요청·페이지네이션을 포함했다. 기존 Operation의 claim·재전달·lease·중복 보고 회귀 테스트도 실행한다.

## 3. 이미지 빌드와 새 서비스 시작

Go·DB 테스트가 통과한 뒤 실행한다.

```bash
cd ~/workspace/agent_project/data_platform
(
  set -euo pipefail
  docker compose --profile jobs build \
    data-operation data-collection data-gateway data-dispatcher data-reconciler crawl-job

  docker compose up -d --no-build \
    data-operation data-collection data-gateway data-reconciler data-dispatcher

  docker compose ps
  docker compose logs --tail=30 \
    data-operation data-collection data-gateway data-dispatcher data-reconciler
)
```

`crawl-job` profile은 이미지 빌드용이다. 상시 실행 서비스로 올리지 않는다. 실제 Job은 Dispatcher가 image ID를 고정하고 시작 승인·CPU 제한을 적용해 생성한다. 첫 시작 시 `data_operations`, `data_collection` DB와 `data-sources` 버킷을 준비한다. Kafka는 데이터 전용 토픽을 사용한다.

`docker compose ps`에서 5개 서비스가 계속 running인지 확인한다. 재시작이 반복되면 로그부터 확인한다. `/health`는 Gateway 프로세스 응답이며 전체 인프라 readiness를 대신하지 않는다.

## 4. 실제 CPU 수집 smoke

GPU를 비우거나 모델 플랫폼을 중지할 필요가 없다. 외부 공개 URL 하나를 실제로 내려받고 원본 파일을 등록하므로 작은 테스트 데이터가 남는다.

```bash
cd ~/workspace/agent_project/data_platform
python tests/live_collection_smoke.py --timeout 600
```

다른 공개 원본 URL을 사용하려면 `--url 'https://example.org/document.pdf'`을 지정한다. 예시 URL의 파일이 실제로 존재하는지는 사용자가 선택한 사이트에 따라 다르다. 기본 `https://example.com/`는 HTML 원본 검증용이다.

검증 항목:

1. 수집 요청 접수와 같은 키 재요청이 같은 ID를 반환.
2. 같은 키의 변경 요청은 409이며 새 Operation이 생기지 않음.
3. 실제 Job이 다운로드·저장·검증·결과 등록을 수행해 성공.
4. Gateway 원본 다운로드가 저장된 SHA-256·크기와 일치.
5. 원본·수집·Operation 목록에서 같은 ID로 조회 가능.
6. 해당 Job의 GPU 요청이 없고 자동 재시작 없이 종료됐으며 실제 image ID가 결과 기록과 일치.

최종 `PASS`와 `[record]` JSON을 확인한다. 로그/원본/중지된 CPU Job은 남는다. 원본 HTML은 attachment로 내려받으며 브라우저에 실행하지 않는다.

## 5. 응답 유실·대기 초과

smoke는 첫 POST 전에 요청·멱등 키를 저장한다. API 응답 유실이나 대기 초과 후에는 새 Job을 만들기 전에 같은 보고서로 이어서 확인한다.

```bash
python tests/live_collection_smoke.py \
  --resume tests/results/collection/실제-record-id.json \
  --timeout 600
```

`--resume`는 보고서에 저장된 API·URL·옵션·키를 그대로 사용한다. 성공·실패한 실행을 되살리지 않는다. 실제 실패 후 의도적인 재수집은 원인을 해결하고 `--resume` 없이 새 요청으로 실행한다. `--timeout 600`은 smoke 대기 시간이며 원격 작업 강제 종료 옵션이 아니다.

## 6. 오류 확인

```bash
curl -fsS 'http://127.0.0.1:14000/api/crawl-runs/실제-crawl-run-id' | jq
curl -fsS 'http://127.0.0.1:14000/api/operations/실제-operation-id' | jq

docker compose logs --tail=80 data-collection data-dispatcher data-reconciler

docker ps -a --filter 'label=data-platform.owner=dispatcher' \
  --format '{{.ID}} {{.Names}} {{.Status}}'
docker logs --tail=80 실제-Job-container-id
```

`LAUNCH_REJECTED`가 이미지 없음이면 `crawl-job` 빌드를 확인한다. 이미 실패한 Operation은 같은 키로 재실행되지 않는다. `CRAWL_FETCH_FAILED`가 HTTP 401/403이면 인증 없는 공개 접근이 가능한 URL인지 확인한다. 링크가 JavaScript 실행 뒤 나타나는 사이트는 이번 HTTP 경로로 탐색되지 않는다.

`verifying_source` 이후 결과 등록 응답이 불확실하면 Reconciler가 등록된 원본을 기준으로 성공을 보정한다. 결과가 끝내 확인되지 않으면 로그에 `manual review`가 남는다. 이 경우 DB 상태·토큰을 직접 바꾸거나 같은 컨테이너를 수동 재시작하지 말고 보고서와 로그를 확인한다. 실제 실행을 자동 반복하지 않는 경계다.

## 확인된 검증 결과

개발 환경과 사용자 서버에서 Python 지원 도구 테스트 8개를 확인했다. 서버 Go 테스트는 최초 실행에서 다른 패키지 60개·하위 사례 40개가 통과했고, 누락된 모델 계약 참조를 수정한 후 멱등 처리 테스트 2개도 통과했다. 합계 62개이며 두 번의 실행 결과를 합산했다.

2026-09-11 04:06 로그에서 전용 서비스 5개 기동과 실제 URL 수집 smoke PASS를 확인했다. 원본은 https://example.com/의 HTML 559바이트다. 파일 저장·다운로드 해시 검증, 멱등 요청·변경 입력 충돌, 목록 조회, CPU Job 종료와 처리 이미지 ID 일치까지 smoke가 확인했다. [실행 ID와 검증 범위](../history/data-platform-202609.md#2026-09-11-03-collection-verified)

실제 장애 주입, 취소, GC, 브라우저 수집·파일 업로드·문서 정제·OCR·UI는 이 성공 결과에 포함되지 않는다.
