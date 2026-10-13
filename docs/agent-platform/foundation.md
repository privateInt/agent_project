# A02 기본 구조·실행 안내

> 이 문서는 A02 인수 당시의 구조/검증 기록이다. 현재 A04 이미지·구성·신규 설치·업그레이드는 [A04 안내](openapi-foundation.md)를 기준으로 한다.

갱신일: 2026-09-22. **서버 smoke·실제 DB 검사·재기동 후 조회 통과로 A02 인수 완료**다. [검증 기록](../history/agent-platform-202609.md#testing-foundation-20260922)에서 실제 실행한 검사와 미실행 범위를 구분한다. 현재 서버는 5절까지 통과했으며 다음 기능 구현은 A03 Tool/MCP다. 아래 설치/복구 절차를 반복할 필요는 없다. 아래 명령은 프로젝트 서버에서 실행한다.

## 1. 이번에 구현한 경계

| 구성 | 구현 내용 | 접근 범위 |
|---|---|---|
| Go Gateway | REST 상태/진단 API, Bearer 인증, 요청 ID, 오류 변환 | Agent 관리 gRPC. DB 비밀 없음 |
| Go 관리 서비스 | 상태 조합, 진단 기록 저장/조회, migration·checksum 검사 | Agent DB와 Agent runtime gRPC |
| Python runtime | 독립 프로세스, 인증된 gRPC Probe/health, 프로세스 UUID, 종료 처리 | A02에는 DB·모델·검색 연결 없음 |
| Agent PostgreSQL 16 | Agent 전용 볼륨·DB·제한된 서비스 계정·schema | Agent private network만 연결, 호스트 포트 없음 |
| DB 검사 컨테이너 | 같은 계약으로 migration 재실행/영속 조회/제한 권한 검사 | 선택적 `checks` profile. 상시 서비스가 아님 |

기존 공유 PostgreSQL을 사용하는 대신 Agent 전용 PostgreSQL을 추가했다. 다른 플랫폼의 DB 계정/기존 PUBLIC 권한을 바꿀 필요 없이 독립성을 유지하기 위한 A02 선택이다. 기존 infra·Model·Data·Knowledge 코드와 설정은 변경하지 않았다. Agent runtime에는 DB 비밀을 주지 않는다. LangGraph checkpoint 계정·저장소는 A06에서 별도로 추가한다.

전용 private network는 `internal: true`다. Gateway와 runtime만 기존 `ops-infra`에도 연결한다. 관리 서비스와 DB는 private network만 사용한다. 기본 외부 포트는 **16100**, 내부 Gateway HTTP는 8000, 관리 gRPC는 9000, runtime gRPC는 9001이다. 16100은 기존 소스의 기본 공개 포트와 겹치지 않지만 서버 점유 여부는 preflight가 확인한다.

Agent 컨테이너에는 Docker socket·GPU·다른 플랫폼 DB/MinIO/OpenSearch 자격을 주지 않는다. 다른 플랫폼/n8n의 기동을 Agent Compose가 제어하지 않는다. Go/Python 서비스는 비root·read-only root filesystem으로 실행한다. A02의 내부 gRPC와 기본 LAN HTTP는 기존 신뢰된 개발망 전제를 따른다. 원격/공개 운영의 TLS·사용자 계정 계약은 별도 범위다.

## 2. 현재 API

| 경로 | 인증 | 동작 |
|---|---|---|
| `GET /` | 없음 | 플랫폼/단계/API 진입점 JSON. 관리 UI는 A08 |
| `GET /healthz` | 없음 | Gateway 프로세스 응답 여부, 200 |
| `GET /readyz` | 없음 | 관리 서비스·DB·runtime 준비 시 200, 아니면 503 |
| `GET /api/status` | Agent API Bearer | 플랫폼/버전/단계, DB/runtime 상태, schema version, request ID. 정상 200/미준비 503 |
| `POST /api/system-checks` | Agent API Bearer | runtime 계약 확인 후 최종 진단을 DB에 저장, 201·Location 반환 |
| `GET /api/system-checks/{check_id}` | Agent API Bearer | 저장된 진단 조회. 200/400/404/503 |

요청 `X-Request-ID`는 표준 소문자 UUID다. 없으면 Gateway가 생성하고 HTTP→Go→Python으로 전달한다. 잘못된 값은 400이며 응답 헤더에도 식별자를 돌려준다. 조회 응답 헤더는 **조회 요청** ID, 진단 본문의 `request_id`는 **원래 생성 요청** ID다.

POST 입력은 비어 있거나 `{}`이며 최대 1024 bytes다. 진단은 `check_id`, `request_id`, `state`, `runtime_instance_id`, `error_code`, 시작/종료 시각만 보관한다. 시작/종료는 저장 전 UTC 마이크로초로 맞춰 생성 응답과 DB 조회 응답의 정밀도를 일치시킨다. runtime 장애도 `state=failed`, `RUNTIME_UNAVAILABLE`로 저장할 수 있다. 이때 HTTP 201은 실패 진단 레코드의 생성 성공이다. 프로토콜/ID 불일치는 `RUNTIME_CONTRACT_MISMATCH`다. DB 저장 자체가 실패하면 성공 응답을 반환하지 않는다. 외부 응답에 내부 DB 오류나 비밀을 싣지 않는다.

진단은 동기 상태 점검이다. **AgentRun·ToolCall·내구성 있는 작업 접수·멱등 키 API가 아니다.** 재전송하면 별도 진단 ID가 생성되며 부작용 있는 외부 API를 실행하지 않는다. A03/A06에서 고유 ToolCall/AgentRun과 멱등/작업자 계약을 추가한다. 내부 health는 토큰을 요구한다. 서비스 health는 DB를 검사하고 전체 준비 여부는 Gateway `/readyz`에서 검사한다.

## 3. 저장소와 버전

- DB `agent_platform`, 서비스 사용자 `agent_service`, schema `agent`.
- 부트스트랩 전용 `agent_admin`은 DB 컨테이너에만 있다. 서비스는 superuser/createdb/createrole/replication/bypassrls 권한 또는 역할 멤버십을 가진 계정으로 시작하지 않는다.
- 서비스 계정은 DB CONNECT와 자기 schema를 갖는다. public schema 접근·DB CREATE 권한을 부여하지 않는다.
- `agent.schema_migrations`: 순서와 SQL SHA-256을 검증하고 transaction/advisory lock 안에서 적용. 기존 migration 변경·새 버전 DB에 대한 구버전 기동은 거부한다.
- `agent.system_checks`: 진단만 저장. 관리 서비스가 실제 쓰기 주체다. 각 진단 ID는 고유하다.
- 전용 볼륨은 서비스 재시작으로 유지된다. 소스 압축 파일은 해당 DB 볼륨의 백업이 아니다.

Go 모듈/생성 코드가 프로젝트에 생기지 않도록 Go Dockerfile에서 생성한다. 직접 SDK 버전은 uuid 1.6.0, pgx 5.10.0, grpc-go 1.83.2, protobuf-go 1.36.12, Go gRPC generator 1.6.2로 고정했다. Python은 gRPC/health/tools 1.84.0, protobuf 7.36.2이며 전체 설치 목록·해시를 `runtime/requirements.txt`에 고정했다. LangGraph/MCP SDK/n8n은 각 구현 단계에서 추가한다.

빌드/실행 기반 이미지 네 개(Go 1.26-bookworm, Python 3.12-slim-bookworm, Debian bookworm-slim, PostgreSQL 16-bookworm)는 registry manifest digest로 고정했다. 정확한 값은 Dockerfile/Compose가 기준이다. 이 digest를 조회한 사실과 실제 컨테이너 빌드/기동 확인은 구분한다. Compose include와 `up --wait`가 필요하며 로컬 설정 해석은 Compose 2.39.4로 확인했다.

## 4. 서버 최초 실행

프로젝트 패키지를 적용한 뒤 다음 명령을 순서대로 실행한다. 호스트 Go/Python venv 설치는 필요하지 않다. Python 3, Docker Engine, Compose 플러그인을 사용한다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
python3 ops/configure.py
python3 ops/preflight.py

docker build --target tests -t agent-platform-go-tests:a02.1 .
docker build -f runtime/Dockerfile --target tests -t agent-platform-python-tests:a02.1 .
docker compose up -d --build --wait
python3 ops/smoke.py

docker compose --profile checks run --build --rm agent-db-checks
)
```

`configure.py`는 Agent `.env`와 비밀 네 개를 신규 생성한다. 기존 파일이 있으면 덮어쓰지 않는다. 한 번 생성한 뒤 재빌드/재기동 때 다시 실행하지 않는다. 포트를 바꿔 처음 설정하려면 `python3 ops/configure.py --port 16101`을 사용하고 smoke에는 `--url http://127.0.0.1:16101`을 준다. 기본 LAN 바인딩 `0.0.0.0`을 유지한다.

`preflight.py`는 서비스 경계·비밀 경로·기존 네트워크·호스트 포트를 읽기 전용으로 검사한다. 네트워크가 없는 독립 신규 환경에서만 `docker network create ops-infra`로 네트워크를 준비한다. 이미 서비스가 올라왔으면 포트를 점유하므로 `python3 ops/preflight.py --skip-port-check`를 쓴다. 서버의 메모리/디스크 여유와 Docker 이미지 빌드 성공도 실제 기동 시 확인한다.

비밀 디렉터리는 host mode 0700, 파일은 0444다. Compose의 파일 secret mount가 호스트 권한을 유지하기 때문에 비root 컨테이너 읽기를 허용하면서 호스트에서는 상위 디렉터리로 접근을 제한한다. `.env`는 0600이며 비밀값은 담지 않는다. 생성 비밀을 다른 사용자/플랫폼에 복사하지 않는다. DB 비밀번호 파일의 내용을 바꾸는 것만으로 기존 볼륨의 비밀번호가 바뀌지는 않는다.

smoke는 건강/준비·미인증 401·상태·진단 생성/재조회를 검사하고 `check_id`를 출력한다. DB 검사 컨테이너는 자체 진단 하나를 추가하고 migration 반복/새 연결 조회/중복 ID 거절/DB 권한을 확인한다. 기존 플랫폼·GPU·문서 인덱스는 호출하지 않는다.

## 5. 재기동 영속성 확인

현재 서버는 아래 검사까지 통과했다. 향후 새 설치/변경 후 검증할 때는 최초 smoke가 출력한 실제 `check_id`를 사용한다. 현재 사용자 서버에서 통과한 ID는 `8a441176-a891-481e-87da-2e73e1762777`이다. 새 설치에서는 해당 설치의 smoke 출력값으로 바꾼다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
docker compose restart agent-postgres agent-service agent-runtime agent-gateway
docker compose up -d --no-build --wait
python3 ops/smoke.py --check-id 8a441176-a891-481e-87da-2e73e1762777
)
```

`--check-id`는 새 진단을 만들지 않고 저장된 기록만 확인한다. 과거 진단의 runtime instance ID는 그대로 남고 새 프로세스는 새 ID를 갖는다. 재시작한 runtime이 과거 실행을 재실행한 것으로 해석하지 않는다.

문제가 있으면 `docker compose ps`와 `docker compose logs --tail=100 agent-postgres agent-service agent-runtime agent-gateway`로 해당 Agent 서비스만 확인한다. DB 초기화 실패나 비밀번호 불일치를 해결하려고 기존 볼륨을 무조건 삭제하지 않는다. 중지에는 `docker compose stop`을 사용한다.

## 6. 다음 단계

서버 smoke/DB/재기동 조회 통과를 [작업계획](work-plan.md)에 반영했고 A02를 완료했다. 다음은 A03 Tool/Version·Connection·공통 호출 기록과 MCP SDK/HTTP 서버다. 이번 진단 엔드포인트를 Tool/Agent 실행 API로 전용하지 않는다.

## 빌드 오류 수정 후 재실행

아래 오류별 복구 절차는 당시 실패 설치를 위한 기록이다. 현재 인수 완료된 서버에서는 반복하지 않는다.

2026-09-22 첫 서버 로그에서 Go 표준 protobuf 정의 누락을 확인해 Dockerfile에 `libprotobuf-dev`와 파일 확인을 추가했다. Python 테스트/런타임 이미지는 빌드 성공했다. 실패·수정 범위는 [검증 기록](../history/agent-platform-202609.md#testing-foundation-20260922)에 보존했다.

최신 패키지를 적용한 기존 A02 설치에서는 아래만 실행한다. 괄호 안의 `set -e`로 첫 실패에서 중단하며 현재 터미널의 설정은 바꾸지 않는다. Dockerfile의 의존성 설치 단계 변경으로 해당 빌드 캐시가 갱신된다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
docker build --target tests -t agent-platform-go-tests:a02.1 .
docker compose up -d --build --wait
python3 ops/smoke.py
docker compose --profile checks run --build --rm agent-db-checks
)
```

설정/비밀을 재생성하거나 볼륨을 지우지 않는다. 성공하면 위의 재기동 영속성 확인을 이어간다.

## 이미지 빌드 성공 후 tmpfs 수정 적용

두 번째 서버 시도에서 Go 테스트와 서비스 이미지 빌드는 성공했다. runtime Compose의 tmpfs 문자열이 쉼표에서 분리돼 컨테이너 생성이 중단됐으며, 문자열 인용과 preflight의 절대 경로 검사로 수정했다. 최신 패키지 적용 후 다음을 실행한다. 성공한 이미지 빌드는 재실행하지 않는다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
python3 ops/preflight.py --skip-port-check
docker compose up -d --no-build --wait
python3 ops/smoke.py
docker compose --profile checks run --build --rm agent-db-checks
)
```

DB 검사 이미지는 이전 단계에서 실행되지 않았으므로 마지막 명령에서 처음 만들 수 있다. 이미 생성된 설정·비밀·DB 볼륨을 사용한다. 컨테이너 기동 성공은 smoke/DB 검사 결과와 함께 기록한다.

## Healthy 이후 진단 결과 비교 오류 수정

세 번째 로그에서 네 서비스 Healthy를 확인했다. 생성 시각의 나노초와 PostgreSQL/pgx의 마이크로초 정밀도 차이로 smoke의 전체 결과 비교가 실패했다. 생성 시작/종료를 저장 전에 마이크로초로 통일했다. 최신 패키지를 적용한 뒤 관리 서비스 이미지만 갱신한다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
docker compose build agent-service
docker compose up -d --no-deps --no-build --wait agent-service
python3 ops/smoke.py
docker compose --profile checks run --build --rm agent-db-checks
)
```

기존 DB/비밀/다른 실행 이미지를 유지한다. schema 변경이나 migration 재작성은 없다. DB 검사도 시각을 포함한 전체 결과 일치 여부를 확인하도록 강화했으므로 검사 이미지는 마지막 명령으로 갱신한다. 두 검사가 통과하면 앞의 재기동 영속성 확인으로 이어간다.
