# 빌드 구조와 서비스별 실행

실행 설정은 서비스별 Compose, 공통 Go 빌드는 루트 Dockerfile, Axolotl은 학습 Job Dockerfile에서 관리한다. 명령은 `~/workspace/agent_project/model_platform`에서 실행한다.

## 파일 책임

| 위치 | 책임 |
|---|---|
| [상위 Compose](../../src/platform/model/docker-compose.yml) | project 이름, 8개 제어 서비스와 선택적 Playground include, 공통 환경 파일 기준과 네트워크 |
| 각 서비스의 `docker-compose.yml` | 해당 서비스 image/build, 환경 변수, 포트, 볼륨, 의존 관계 |
| [공통 Dockerfile](../../src/platform/model/Dockerfile) | Go 의존성·protobuf·공통 코드와 서비스별 컴파일/최종 이미지 |
| [Playground Dockerfile](../../src/platform/model/playground/Dockerfile) | Streamlit 체험 UI, 별도 Python 이미지 |
| [Axolotl Dockerfile](../../src/platform/model/jobs/training/axolotl/Dockerfile) | CUDA·Python·Axolotl·MLflow 런타임 |
| [docker-bake.hcl](../../src/platform/model/docker-bake.hcl) | Job 이미지와 테스트 이미지의 빌드 의존 관계 |
| [장애 테스트 Dockerfile](../../src/platform/model/tests/dispatch_fault/Dockerfile) | 격리 테스트용 실제 제어 바이너리와 helper 묶음 |

`include`마다 `project_directory: .`, `env_file: ./.env`를 명시했다. 하위 Compose의 `build.context: .`는 `src/platform/model`을 가리킨다. 환경 파일을 서비스 폴더마다 복사하지 않는다. 공통 네트워크는 상위에서 한 번 선언하고 자식들이 참조한다. 하위 Compose는 상위 파일을 통해 사용하는 구성 조각이며, 각 폴더에서 독립 프로젝트로 실행하는 용도가 아니다. [Docker include 규칙](https://docs.docker.com/reference/compose-file/include/)

Compose project 이름 `model-platform-e2e`, 서비스 이름, container_name, 주소·포트·소켓 연결은 기존과 같다. 이미지 이름만 서비스별로 구분한다. 로컬 소스로 빌드하므로 `pull_policy: never`를 사용하고 기동 전에 명시적으로 빌드한다.

## 빌드 캐시와 이미지

Go 의존성 설치는 소스를 복사하기 전에 수행한다. contracts가 바뀌면 protobuf를 다시 생성하며, 그 뒤 internal과 해당 서비스 소스만 복사해 개별 바이너리를 컴파일한다. Operation 수정만으로 Management 전용 소스 복사·컴파일 단계가 무효화되지 않는다. 공통 internal/contracts 변경은 영향을 받는 공통 단계를 갱신한다.

컴파일에는 Go build cache mount를 사용한다. module 다운로드는 이미지 레이어에 남겨 `builder` 컨테이너의 테스트에서도 사용할 수 있게 한다. `go.mod/go.sum`은 빌드 이미지 안에서 생성한다. `builder`는 전체 소스와 생성된 contracts를 가진 테스트 환경이고, 각 프로덕션 이미지는 이 전체 소스 단계에 의존하지 않는다. [Docker 캐시 원칙](https://docs.docker.com/build/cache/optimize/)

| 서비스 | 이미지 |
|---|---|
| gateway | `model-platform-gateway:dev` |
| management | `model-platform-management:dev` |
| operation | `model-platform-operation:dev` |
| training | `model-platform-training:dev` |
| deployment | `model-platform-deployment:dev` |
| dispatcher | `model-platform-dispatcher:dev` |
| reconciler | `model-platform-reconciler:dev` |
| gc | `model-platform-gc:dev` |
| playground (선택적 profile) | `model-platform-playground:dev` |

8개 제어 이미지에는 해당 Go 실행 파일 하나가 들어간다. Playground는 별도 Python/Streamlit 이미지이며 `docker compose --profile playground build playground`로 빌드한다. Docker CLI는 training/deployment/dispatcher/reconciler/gc에 넣는다. Training은 취소 시 공유 GPU inventory를 확인하고, Deployment는 별도 vLLM 컨테이너의 생성·조회·중지를 담당한다. `gc-test` target은 실제 Docker/MinIO 통합 검증을 위해 builder에 Docker CLI를 추가한다. 테스트용 fault 이미지에는 필요한 제어 바이너리 여러 개를 넣지만 정상 서비스가 사용하지는 않는다.

## 전체 빌드

먼저 실제 Docker 파서로 include 경로, 환경 변수 해석, Job tag와 named context를 검사한다. 검사기는 환경 값을 출력하지 않고 컨테이너를 만들지 않는다.

```bash
cd ~/workspace/agent_project/model_platform
python tests/check_build_layout.py
```

다음 명령은 이미지 빌드만 수행한다.

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build
  docker buildx bake --builder default -f docker-bake.hcl --load jobs
)
```

`docker compose build`는 기본 제어 서비스 8개를 빌드한다. Playground는 profile을 지정해 선택 빌드·기동하며 [체험 페이지 적용](../testing/streamlit-playground.md)을 따른다. GC의 Compose 기본값은 apply(성공 1시간·실패 7일 보존)이며 모드와 검증은 [GC 안내](gc.md)를 따른다. Bake의 `jobs`는 import·training·demo deployment·serving coordinator의 네 태그를 빌드하고 `--load`로 로컬 Docker에 등록한다. 실제 vLLM 이미지는 별도로 pull한다. Compose에는 Job 서비스를 정의하지 않아 `up`으로 학습이 시작되지 않는다.

Axolotl은 Bake named context `training_binary=target:training-binary`로 같은 소스에서 만든 Go 실행 파일을 받는다. 이전 로컬 builder 이미지 태그를 복사하는 방식이 아니며, 필요한 컴파일을 같은 빌드 그래프에서 처리한다. Bake는 기존 Docker Buildx의 선언형 빌드 기능이다. [named context 문서](https://docs.docker.com/build/bake/contexts/)

## 서비스 또는 Job 하나 수정

Operation만 수정한 경우:

```bash
cd ~/workspace/agent_project/model_platform
(
  set -euo pipefail
  docker compose build operation
  docker compose up -d --no-build --no-deps operation
  docker compose logs --tail=30 operation
)
```

수정 범위에 맞는 [회귀 테스트](../testing/README.md)를 먼저 통과시킨다. 공유 계약 변경은 관련 서비스까지 함께 빌드·배포한다.

학습 런타임만 빌드하려면:

```bash
cd ~/workspace/agent_project/model_platform
docker buildx bake --builder default -f docker-bake.hcl --load training-job
```

다른 Job은 `management-import-job`, `deployment-job`, `deployment-serving-job` target을 사용한다. 기존 Job 태그는 `launch-v1`, 실제 서빙 coordinator는 구버전 demo 이미지와 구분하기 위해 `model-platform-deployment-job:serving-v1`이다. 새 요청부터 새로 빌드한 이미지를 사용하고 이미 실행 중인 컨테이너는 기존 이미지로 계속 실행된다. 서빙 런타임 이미지·캐시·새 서비스 전환 순서는 [서빙 절차](../testing/serving.md)를 따른다.

`docker build --target control`과 루트 Dockerfile의 `--target training-job`은 더 이상 현재 빌드 진입점이 아니다. 상시 서비스는 Compose target, 학습은 Bake target을 사용한다. 장애 실험은 [현재 테스트 절차](../testing/request-creation.md#dispatch-fault)를 따른다.
