# Model Platform 관리 수정 적용·검증

기준일: 2026-09-18. [동작/API 계약](../reference/model-management-update.md).

## 현재 확인한 범위

- 로컬: 기존 Python 25개 + 새 UI 요청/진행률/삭제 경계 검사 5개 통과. Python 문법 검사 통과.
- 최초 로컬 Streamlit 42개는 SKIP이었으나, 사용자 제공 서버 로그에서 Python 25개·5개·AppTest 42개 모두 통과했다.
- 새 Go 검사: 이름 변경 후 UUID·artifact·배포 연결 유지, 중복/동시 편집 충돌, 입력 검증; 실제 PostgreSQL 배포 삭제·부분 실패·재시도·목록 제외·controller lock; Docker CLI 대역의 전체 ID/비강제 삭제; REST 응답/오류 매핑.
- 사용자 로그에서 management/app, deployment/app, deployment/runtime, gateway/app, tests/creation의 Go/DB 검사와 네 서비스 이미지 빌드·교체를 확인했다. 실삭제에서 발생한 Unavailable은 후속 수정 뒤 사용자 성공 확인을 받았다. 원인 및 미적용 환경의 최소 반영 절차는 아래 후속 수정 항목을 따른다. 새 후속 코드의 Go 실행은 현재 작업 환경에 Go/Docker가 없어 미실행이다.

**현재 재개 기준:** 사용자 “이제 삭제 된다” 응답으로 후속 수정 뒤 기능 성공을 확인했다. 아래 절차는 설치·회귀 시 참고용이며, 다음 Knowledge 작업 전에 같은 삭제나 전체 빌드를 다시 요구하지 않는다. 새 후속 테스트별 로그와 cache 파일시스템 상세는 별도 미수령이다.

## 1. Go·PostgreSQL 검사

프로젝트 소스를 증분 패키지로 적용한 뒤 실행한다. 테스트는 임시 schema와 가짜 runtime을 사용하며 실제 모델을 띄우거나 기존 배포를 삭제하지 않는다. DB role에는 임시 schema 생성·삭제 권한이 필요하다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker build --target builder -t model-platform-check:management-update .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:management-update \
    go test -count=1 -v ./management/app ./deployment ./deployment/app \
      ./deployment/runtime ./gateway/app ./tests/creation
)
```

DB 테스트가 `OPERATION_TEST_POSTGRES_URL is not set`으로 SKIP되면 완료가 아니다. 실패하면 해당 출력부터 확인하고 서비스 교체 단계로 진행하지 않는다.

## 2. 이미지·UI 검사

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build management deployment gateway
  docker compose --profile playground build playground
  for suite in tests/test_playground.py tests/test_model_management_ui.py tests/test_playground_ui.py; do
    docker run --rm -i -e PLAYGROUND_SOURCE_DIR=/app \
      model-platform-playground:dev python - -v < "$suite"
  done
)
```

이 이미지에서는 Streamlit 42개가 실제 실행되어야 한다. API는 대역이므로 브라우저 타이머·레이아웃과 실제 서버 검증을 대신하지 않는다.

## 3. 서비스 교체

위 두 검사 모두 성공한 뒤 실행한다. 가져오기·학습·배포 시작 작업이 진행 중이면 완료 후 실행한다. 관리 서비스 교체 중 짧은 API 공백이 생긴다. 이미 실행 중인 별도 `model-serving-*` 컨테이너는 아래 Compose 교체 대상이 아니다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose stop dispatcher
  trap 'docker compose start dispatcher' EXIT
  docker compose up -d --no-build --no-deps --force-recreate management deployment
  docker compose up -d --no-build --no-deps --force-recreate gateway
  docker compose --profile playground up -d --no-build --no-deps \
    --force-recreate --wait --wait-timeout 60 playground
  docker compose --profile playground ps management deployment gateway playground
)
```

Deployment 기동 시 nullable `deleted_at` 열을 추가한다. `.env`, 볼륨, 모델 파일, 기존 Knowledge/Data Job은 바꾸지 않는다. importer·Operation·Training·vLLM 이미지를 재빌드하지 않는다. UI만 교체하면 새 API가 없으므로 네 서비스를 함께 교체한다. 삭제 후 구버전 소스로 되돌리면 tombstone이 목록에 다시 나타날 수 있어 이번 Deployment/Gateway 조회 계약을 유지해야 한다.

## 4. 화면 확인

1. 모델 조회 → **이름 수정** → 등록 이름 저장. 같은 모델 ID, repo/revision이 유지되고 목록·배포 목록의 이름이 갱신되는지 확인한다.
2. 원하는 버전을 선택하고 이름 수정을 열어 **버전 이름 저장**. UUID와 파일을 유지하며 드롭다운 라벨만 바뀌어야 한다. 한글·중복 이름·빈 이름과 다른 창의 동시 편집도 확인한다.
3. 다음 필요한 모델 가져오기 중 상태 아래 현재 파일 %·저장 파일 수가 주기적으로 바뀌는지 확인한다. 진행률 확인만을 위한 대용량 중복 다운로드는 필요 없다.
4. 필요 없는 중지된 배포 하나를 선택한다. 로그가 필요하면 삭제 전에 별도 보관한다. **삭제 → 배포 삭제** 후 그 행만 사라지고 등록 모델은 남아야 한다. 해당 `model-serving-<배포 UUID>`만 없어졌는지 확인한다. `model-op-*` 정리 여부로 이 기능을 판정하지 않는다.
5. 실행 중인 배포의 삭제 버튼이 비활성인지 확인한다. REST로 직접 DELETE해도 409여야 한다. 삭제 도중 실패하면 목록을 성공으로 숨기지 않고 동일 대상 재시도가 가능해야 한다.

소스 검사, 실제 API·DB, 실제 Docker 정리, 브라우저 확인을 각각 기록한다. 결과는 [1 MB 합산 보관 기준](artifact-retention.md)을 따른다.

## 배포 삭제 Unavailable 후속 수정

2026-09-18 서버 로그에서는 최초 41개 적용, 다섯 Go package 검사, Python 25+5+42개, management/deployment/gateway/playground 교체까지 성공했다. 이후 사용자가 중지된 배포 b35f856b 삭제에서 `Unavailable`을 보고했다.

**원인:** `deployment/main.go`가 `serving.FetchFunc`로 Prepare만 주입했다. 실서비스 객체에는 `Purge(ctx, id)`가 없으므로 DeleteDeployment의 capability 검사에서 Docker·cache 삭제를 시작하기 전에 `runtime cleanup is unavailable`로 거절한다. 기존 테스트는 SnapshotFetcher/삭제용 대역을 직접 주입해 실제 기동 구성을 검사하지 못했다. 모델 파일·컨테이너 손상이나 GPU OOM을 이 오류의 원인으로 판단하지 않는다.

**수정:** 실서비스에 `servingFetcher`를 연결해 Prepare는 기존 지연 MinIO 연결을 사용하고, Purge는 같은 cache root의 SnapshotFetcher.Purge로 위임한다. 캐시 삭제는 MinIO 연결 없이 수행하며 UUID·symlink·취소 검사를 유지한다. 기존 모델 파일 purge도 같은 객체를 쓰므로 이 연결 수정이 함께 적용된다. 새 production constructor 회귀 테스트 2개를 추가하고 deployment 이미지 빌드 시 `go test -count=1 ./deployment ./deployment/runtime`을 필수 실행한다.

후속 증분 소스를 적용한 뒤 **deployment만** 빌드·교체한다. 초기 관리 수정의 나머지 서비스·UI/DB 전체 검사를 다시 할 필요는 없다. 아래 build가 새 Go 테스트를 실행하며 실패하면 교체하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build deployment
  docker compose up -d --no-build --no-deps --force-recreate deployment
  docker compose ps deployment
)
```

화면에서 **배포 조회 → 같은 중지된 배포 삭제**를 다시 실행한다. 최초 capability 거절은 삭제 전 단계이며 같은 ID DELETE를 재확인할 수 있다. 컨테이너·전용 cache 제거와 행 제외가 확인되면 성공이다. 등록 모델은 계속 남아야 한다. 진행 중인 다운로드/배포 준비가 있으면 완료 후 서비스 교체한다. 기존 model-serving 런타임을 수동 rm/prune하거나 DB 행을 지우는 우회 조치를 하지 않는다.

오류가 계속되면 실제 API 오류 메시지로 구분한다. 아래 조회는 실행 중인 배포나 모델을 삭제하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
docker compose logs --tail=80 deployment gateway
```

후속 확인: 사용자가 삭제 성공을 보고했다. 새 Go/빌드의 개별 로그와 cache 파일시스템 상세를 추가로 받은 것은 아니므로 그 검사를 별도 PASS로 표시하지 않는다. 기존 Go/DB/UI 로그, 후속 사용자 기능 확인, GC 보존 상태를 구분해 `model_platform/tests/results/model-management-update-20260918.json`에 요약했다.


<a id="redeploy"></a>

## 중지된 모델 재배포

- 종료가 확인된 vLLM 배포의 `종료` 자리에서 `재배포`를 선택한다. 상세 보기에서도 선택할 수 있다.
- 기존 모델 버전 ID·서빙 profile·최대 길이·GPU 메모리 비율·GPU ID·노드 설정을 표시한다. `같은 설정으로 새 배포`를 눌러야 요청한다.
- 기존 등록 모델 파일로 **새 배포 ID·새 작업·새 runtime**을 만든다. 중지된 배포와 로그는 남으며, 원하면 기존 삭제 기능으로 정리한다. 모델을 Hugging Face에서 다시 가져오지 않는다.
- OCR `surya-ocr-v2`, embedding `snowflake-ko-v1`, reranker `ko-reranker-v1`, 기본 vLLM 및 adapter의 저장 설정을 보존한다. 기존 blank-profile embedding도 빈 profile 그대로 요청한다. 최신 모델 버전을 임의 선택하지 않는다.
- 새 배포 ID는 기존 배포 ID를 대체하지 않는다. Data OCR이나 Knowledge 등 연결 서비스에서는 새 배포를 선택하고, embedding/reranker는 새 profile을 조회해 호환성을 확인한다. 기존 인덱스의 embedding 공간을 새 모델로 자동 변경하지 않는다.

`status/runtime_state/desired_state`가 모두 `stopped`이고 삭제되지 않은 배포만 재배포할 수 있다. 모델 파일이 삭제됐거나 현재 모델 버전·설정이 지원되지 않으면 기존 서버 검증에서 거절한다. 이미지 선택은 현재 Model 설정을 따르므로 과거 runtime image와 동일하다고 보장하지 않는다. GPU 자원 확보와 실제 모델 로딩 성공은 새 배포의 상태로 확인한다.

### 구현과 확인 범위

새 REST/RPC나 DB 필드는 추가하지 않았다. 새 배포는 기존 `POST /api/deployments`와 Operation 경로를 사용하며 OCR profile은 기존 OCR 생성 경로로 분기된다. 이전 container를 `docker start`하지 않는다.

선택·확인 시 상세를 재조회해 상태·설정 변경을 검사한다. 표시 이름이나 화면에서 선택한 다른 모델 버전을 요청에 사용하지 않는다. 응답 유실 시 기존 요청 키·본문을 보존하고 사용자의 `같은 요청 다시 확인`으로 결과를 조회한다. 자동 재전송은 없다. 브라우저 세션이 바뀌면 같은 중지 배포를 다시 선택하고 동일한 설정으로 확인해 URL에 보존된 요청 키를 재사용한다. 결과 확인 전 요청 기록을 닫고 새 요청을 만들면 중복 배포가 생길 수 있다.

실제 Streamlit 1.63.0 AppTest **52개**, 관리 경계 **7개**, 요청·상태 로직 **27개**, 합계 **86개**가 로컬에서 통과했다. 새 재배포 검사는 OCR·reranker·embedding·adapter/blank profile 설정 보존, 취소, 상태 변경·삭제·설정 변경 거절, 응답 유실·URL 복원 시 같은 키 사용, 서버 변경 거절을 포함한다. AppTest의 Gateway 응답은 대역이며 실제 Docker 이미지·GPU 재배포는 아직 확인하지 않았다. [검증 요약](../../model_platform/tests/results/model-redeploy-20260921.json).

화면에서 `배포 조회 → 중지된 배포의 재배포 → 같은 설정으로 새 배포`를 선택하면 새 배포 상세로 이동하고 기존 행은 중지 상태로 남는다. 실패하면 새 배포 오류를 확인한다. 현재 정상 embedding/reranker를 시험 목적으로 중지하지 않는다. 실제 재배포가 필요한 경우에 확인하고 결과를 별도로 기록한다.


<a id="import-progress"></a>

## 다운로드 진행률 검증

진행률 관련 서버 Go/DB와 당시 AppTest 35개 통과는 이력이다. 최신 UI 전체 검사는 [Playground](streamlit-playground.md), 실제 다운로드의 타이머·중간 표시 관찰은 필요한 모델을 가져올 때 확인한다. 옛 Job이나 빠르게 끝나는 작은 파일에는 중간 퍼센트가 보이지 않을 수 있으며 이를 보기 위해 중복 다운로드하지 않는다.

### 검증과 반영 범위

현재 진행 중인 모델 다운로드를 완료한 뒤 2단계 서비스를 교체한다. 파일 적용·빌드·테스트는 실행 중인 컨테이너의 소스를 교체하지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker build --target builder -t model-platform-check:import-progress .
  OPERATION_TEST_POSTGRES_URL=$(
    docker compose config --format json |
      jq -er '.services.operation.environment.POSTGRES_URL'
  )
  export OPERATION_TEST_POSTGRES_URL
  docker run --rm --network ops-infra \
    -e OPERATION_TEST_POSTGRES_URL \
    model-platform-check:import-progress \
    go test -count=1 -v ./operation/app ./internal/operationreport \
      ./gateway/app ./jobs/management/import_huggingface ./tests/creation
)
```

PostgreSQL 테스트가 SKIP되지 않았는지 확인한다. 테스트는 격리 schema를 사용하며 실제 GPU/모델 다운로드는 수행하지 않는다. importer 테스트의 HTTP·MinIO는 대역이며 파일 바이트와 해시·실패 시 저장 개수를 검사한다.

진행률 코드 변경은 Operation·Gateway·import Job·Playground가 함께 반영돼야 한다. 진행 중인 다운로드가 끝난 뒤 서비스 교체를 수행하고 전환 중 Dispatcher의 새 Job 시작을 제어한다. 이미 실행 중인 Job은 새 이미지로 바뀌지 않는다. 빌드/교체 기본 절차는 이 문서와 [빌드 운영](../operations/build.md)을 따른다. 진행률만을 위해 Management·Training·Deployment·학습/vLLM 이미지를 교체하지 않는다. Operation의 선택적 progress_json/progress_updated_at 컬럼과 비root UI 검사도 확인한다.

### 해석과 조회

퍼센트는 **현재 파일의 다운로드 바이트 / HTTP 응답에서 확인한 파일 크기**다. 전체 작업 퍼센트가 아니며 다음 파일에서 다시 시작한다. 전체 파일 크기를 모르면 바이트만 표시한다. 파일별 MinIO Put 성공 뒤에만 저장 완료 개수를 증가시킨다. 업로드는 전송량 퍼센트 대신 저장 중 상태를 보여준다. 모든 파일을 저장한 뒤에도 manifest와 Management 등록 완료를 기다리며, 최종 성공은 Operation succeeded와 모델 ready로 확인한다.

HTTP 길이 미상·옛 Job·전송 보고 지연 때 가짜 퍼센트를 표시하지 않는다. 실패 이력에는 마지막 전송량이 남는다. 다운로드/화면 갱신이 새 작업을 자동 생성하지 않는다. 진행 보고는 현재 실행 승인 token/instance로만 쓰며, 과거 sequence와 완료 후 보고가 최신 상태를 바꾸지 못한다. 조회 API는 해당 비밀을 반환하지 않는다.

다음 명령의 ID를 실제 선택 버전의 작업 상세에서 확인한 Operation ID로 바꾼다. 조회만 수행한다.

```bash
IMPORT_OPERATION_ID='작업 상세의 operation_id'
curl -fsS "http://127.0.0.1:13000/api/operations/$IMPORT_OPERATION_ID" |
  jq '{operation_id,status,phase,progress,progress_updated_at,error_code,error_message}'
```

TRANSFER_FAILED와 download ... status 401/403이면 모델 접근 승인을 받은 계정의 읽기 토큰을 확인한다. 호스트 HF 로그인은 자동 사용하지 않는다. 이미 실패한 버전은 같은 모델 상세의 다른 버전 추가에서 새 버전 이름과 토큰으로 요청한다.

### 유지되는 동작

모델의 목록 삭제(DELETE)는 파일·이력·디스크 공간을 보존한다. 현재 화면에는 별도의 파일 영구 삭제도 있으며 [purge 계약](../reference/catalog.md#파일-영구-삭제)을 따른다. 삭제한 이름은 재사용할 수 없고 복구 API는 없다. 배포 종료와 학습 취소는 해당 작업을 선택해 명시적으로 실행한다. 응답 유실은 원래 입력과 같은 요청 키로 확인하며 팝업/페이지 새로고침이 자동 재요청하지 않는다. 체크포인트 재개·출력 복구·평가·채팅은 기존 계약을 유지한다. 기존 GPU 학습/서빙 smoke를 이번 UI 변경 때문에 반복할 필요는 없다.
