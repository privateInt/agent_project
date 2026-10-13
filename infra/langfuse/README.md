# Langfuse 전용 인프라 — O01

**2026-09-28 최우선 설계 원칙:** [자동화와 적응성](../../docs/design-principles.md). 사용자 설정·반복 조작을 최소화하고, 환경과 대상을 자동 발견하여 연결 정보를 생성·갱신하는 것을 전 플랫폼 공통 목표로 확정했다. 수동 설정 분리는 기반이며 자동 발견 완료와 구분한다.

**2026-09-28 관측 환경 분리 서버 확인:** [근거·현재 범위](../../docs/history/ops-evidence-catalog.md#portability-20260928). 10:24 KST Ops `o04.3` Healthy, 네 플랫폼·자원 7개 그룹·GPU 지표 6개·원래 Agent 실패/추적 10개 조회 PASS, 사전 조사 컨테이너 9개 matched와 키 권한 오탐 해소를 확인했다. 사전 조사 전체는 DB/로그 크기 권한 부족과 T91 읽기 전용으로 attention_required다. 다음은 공통 원칙에 따른 환경·서비스 발견/자동 연결 계약 구체화이며 Kubernetes/PVC 조사·백업/복원은 미구현/미실행이다. 완료한 빌드·재시작·조회·추론을 반복하지 않는다.

**2026-09-26 Langfuse 재시작 보존 서버 인수 PASS:** 읽기 전용 diagnose/compare로 원래 6개 컨테이너의 재시작·Healthy, 같은 ID/이미지/설정/저장 연결과 고정 trace 3개 span 보존을 확인했다. 기준 hash도 유지됐고 이번 조회의 추가 재시작·POST·추론은 없었다. 현재 차이 항목과 순서 동등성 적용 목록은 모두 비어 있어 최초 비교 오류의 원인은 미확정으로 남긴다. [서버 근거·완료 범위](../../docs/history/ops-platform-202609-20261001.md#langfuse-restart-verified-20260926). 당시 다음 기능인 O02 Agent 계측과 Ops 핵심 화면 확인도 후속으로 완료했다. 현재 후속은 상단 관측 복구 계획이며 완료한 인프라 검사는 반복하지 않는다.

2026-09-26 구현. Langfuse **4.46.0** Web/Worker와 PostgreSQL·ClickHouse·Redis·MinIO의 선택 기동, headless 초기 설정, 합성 추적 수집/조회 도구다. 로컬 Python/HTTP 대역·Compose 해석 검사 완료. **6개 Healthy·합성 trace 수집/재조회 서버 인수 PASS**다. 고정 trace 재시작 보존도 위 서버 인수까지 완료했다. [서버 결과와 원래 ID](../../docs/history/ops-platform-202609-20261001.md#langfuse-verified-20260926)를 보존한다. 이번 문서 갱신 때문에 기동·합성 검사를 반복할 필요 없다. Agent 계측과 ops 플랫폼 연동은 다음 단계다.

## 고정 추적 재시작 보존 — 서버 인수 완료

[서버 기록](../../docs/history/ops-platform-202609-20261001.md#langfuse-restart-verified-20260926)에서 6개 서비스와 원래 trace의 재시작 보존을 확인했다. 이번 문서 적용 후 재검사는 필요 없다. 아래는 최초 검증 절차를 보존한 참고 명령이며 현재 서버에서 다시 실행할 작업이 아니다.

```bash
python3 -B infra/langfuse/ops/restart_check.py run --wait-seconds 180
python3 -B infra/langfuse/ops/restart_check.py compare --wait-seconds 60
```

첫 run은 원래 기록·3개 span·6개 Healthy·인증 프로젝트를 확인하고, 상태 폴더에 `restart-acceptance-o01.json`을 원자적으로 저장한 뒤 6개 기존 컨테이너 ID만 한 번 재시작한다. 같은 기준이 있으면 run도 읽기만 수행한다. compare는 기준/서비스/HTTP 조회만 한다. 설정/키/원래 `trace-check.json`을 수정하거나 새 trace/모델 호출을 만들지 않는다. 원래 컨테이너·image ID·설정·저장 연결, 새로운 시작 시각, 3개 span의 원본 필드 보존과 API 복귀를 확인한다.

인수 조건은 `passed`, 서비스 6개 Healthy/재시작 관측, `observations_verified: 3`, 같은 trace ID·payload/baseline hash여야 한다. `pending`/exit 2이면 기준을 유지하고 compare로 조회한다. 응답 유실이나 명령 전 중단에서 재시작이 0개/일부만 관측될 수 있으며 자동으로 다시 재시작하지 않는다. 이때도 기준을 삭제하지 말고 서비스 상태를 진단한다. 전체 저장소 백업/복원 검사는 아니다.

기존 38개와 새 재시작 검사 12개를 합친 50개 로컬 검사가 통과했다. 실제 HTTP 대역과 Docker 명령 대역으로 검증했으며 후속 보완 포함 56개 로컬 검사와 위 사용자 서버 compare PASS를 별도 근거로 유지한다.

## 2026-09-26 Web/Worker 바인딩 복구 — 서버 PASS

서버 진단에서 PostgreSQL·ClickHouse·Redis·MinIO는 Healthy, Web·Worker는 running/unhealthy이며 OOM·재시작·상태 오류는 없었다. 저장 경로 5곳은 모두 원래 연결을 유지했다. Worker 로그는 주기 작업을 처리하고 있으며 두 앱의 기존 헬스체크 출력은 비어 있었다. 이후 앱의 HTTP 바인딩 비교에서 두 앱 모두 loopback는 연결 거절, 컨테이너 주소는 HTTP 200으로 원인을 확인했다. 최초 `set -e` 중단 때 미실행이던 합성 trace run/check도 복구 후 모두 passed였다.

Compose의 Web/Worker에 `HOSTNAME` 명시가 빠져 있었다. 고정 v4.46.0 Worker는 `app.listen(env.PORT, env.HOSTNAME)`을 사용한다. Docker가 설정한 컨테이너 hostname에만 수신하면 `127.0.0.1` 헬스체크와 맞지 않는다. 공식 설정도 컨테이너 수신에는 `HOSTNAME=0.0.0.0`을 안내한다. 이 구성 누락을 수정했다. **실제 서버에서 주소 불일치 확인·Web/Worker만 재생성·6개 Healthy·인증된 프로젝트 연결·설정/키/저장소 보존·합성 run/check까지 통과했다.** 아래 명령은 완료한 복구 절차의 기록이다. 문서 적용 후 다시 실행할 필요 없다.

```bash
python3 -B infra/langfuse/ops/manage.py repair-health --wait-seconds 180
python3 -B infra/langfuse/ops/trace_check.py run --wait-seconds 120
python3 -B infra/langfuse/ops/trace_check.py check --wait-seconds 120
```

`repair-health`는 기존 비공개 상태·소유권·이미지 digest·저장 연결과 4개 저장소 Healthy를 검사한 다음, 각 앱 내부에서 loopback와 실제 컨테이너 hostname에 동일한 health/readiness GET을 보낸다. loopback가 `ECONNREFUSED`인데 컨테이너 주소에서는 모든 endpoint가 HTTP 200일 때만 바인딩 불일치로 판정한다. 예상 밖 HOSTNAME/HTTP 503/저장소 장애는 자동 수정하지 않는다. 출력에는 endpoint 이름과 HTTP 상태/오류 코드만 포함한다. 인증 헤더·환경 값·응답 본문을 출력하지 않는다.

원인이 확인되면 `up --no-deps --no-build --pull never --wait`로 해당 Web/Worker만 재생성한다. 강제 재생성은 쓰지 않으며 이미 수정된 앱은 반복하지 않는다. `HOSTNAME=0.0.0.0`은 Compose의 두 앱 환경에만 추가한다. 초기 입력 주소 `http://localhost:16320`과 인증 callback, 기존 `*.env`/settings/키, 6개 이미지 digest, 저장소·포트·네트워크는 유지한다. 정상 헬스체크의 readiness·DB·Worker event propagation 조건도 유지한다. 실패 시 빈 출력 대신 안전한 상태 코드를 남긴다.

복구 완료는 6개 Healthy, 실제 Langfuse 버전, 인증된 프로젝트 연결, 저장소 4개의 컨테이너/이미지/마운트/시작 시각·재시작 횟수와 원래 설정 바이트 보존을 확인한 후 `result: passed`다. 추적 전달은 별도 `trace_check.py`의 같은 trace ID·hash 및 `observations_verified: 3`으로 판정한다. 데이터 삭제·이미지 pull·Agent/n8n/관측 서비스 재시작·모델 추론은 수행하지 않는다. 실패하면 상태 폴더를 지우지 않고 출력과 아래 진단을 확인한다.

```bash
python3 -B infra/langfuse/ops/manage.py diagnose
```

진단은 소유한 컨테이너의 상태와 최근 헬스체크/로그를 비밀을 가려 읽는다. `diagnosed`는 정보 수집 결과이며 인수 PASS가 아니다. 초기 실패 당시 원문 Compose 오류는 남아 있지 않아 소급 복구할 수 없다. 이후 Compose 실패는 비밀을 가린 원문과 진단을 출력하며 실패 코드를 유지한다.

로컬 Python 38개 검사(기존 31개 + 복구/Node HTTP 7개), Compose 2.39.4 실제 해석과 Python 3.9 문법 검사를 통과했다. Node 24.19.0과 두 로컬 주소의 HTTP fixture로 바인딩 불일치/전체 주소 수신 복구, 실제 배포 헬스체크 JS의 200·503·연결 거절 처리를 검증했다. HTTP fixture는 실제 Langfuse 제품이나 Docker 통합 검사가 아니다. 별도 Compose 전후 비교에서 저장소 4개·이미지·마운트·네트워크·포트·비밀 해석은 같고 두 앱의 HOSTNAME/healthcheck만 달랐다.

근거: [v4.46.0 Worker 수신 코드](https://github.com/langfuse/langfuse/blob/v4.46.0/worker/src/index.ts), [Worker 환경 계약](https://github.com/langfuse/langfuse/blob/v4.46.0/worker/src/env.ts), [공식 HOSTNAME/PORT 설정](https://langfuse.com/self-hosting/configuration), [health/readiness](https://langfuse.com/self-hosting/configuration/health-readiness-endpoints).

## 시작

프로젝트 루트에서 실행한다. 기존 `infra/docker-compose.yml`에 자동 include하지 않으며 `--langfuse-only`로만 선택 기동한다. 일반 infra/n8n/관측/GPU 기동 동작은 유지한다.

```bash
python3 -B infra/ops/up.py --langfuse-only
python3 -B infra/langfuse/ops/trace_check.py run --wait-seconds 120
python3 -B infra/langfuse/ops/trace_check.py check --wait-seconds 120
```

첫 기동에서 접속 주소·이메일·표시 이름·비밀번호를 한 번 입력한다. 접속 주소는 `http://<서버IP>:16320`이며 이 주소를 Langfuse 인증 callback에도 사용한다. LAN에서 사용할 예정이면 서버의 LAN IP를 입력한다. 기본값 `http://localhost:16320`은 서버 로컬 또는 SSH 터널용이다. 초기화 후 주소를 바꾸는 자동 재설정 기능은 이번 범위에 없다. 비밀번호는 8자 이상·UTF-8 72바이트 이하이며 입력은 표시하지 않는다. 초기 계정·조직·프로젝트·프로젝트 키는 공식 `LANGFUSE_INIT_*` 계약으로 생성한다. 일반 회원가입은 비활성화한다.

**페이지를 열지 않아도 위 세 명령으로 기능 인수가 가능하다.** `up`은 `result: ready`, 합성 `run`과 읽기 전용 `check`는 같은 trace ID/hash로 `result: passed`, `observations_verified: 3`이면 된다. 오류 표시용 `synthetic-error` span 하나는 의도한 검사 데이터다. 실제 LLM/임베딩/검색은 호출하지 않는다.

설정만 미리 준비할 경우:

```bash
python3 -B infra/langfuse/ops/manage.py prepare --public-url http://<서버IP>:16320
```

`<서버IP>`를 실제 주소로 바꾼다. 이 선택 명령 뒤에는 위 시작 명령을 실행하면 되며 같은 계정 정보를 다시 묻지 않는다. 자동 공개 TLS/리버스 프록시 설정은 이번에 포함하지 않는다.

## 격리와 저장

| 항목 | 이번 구성 |
|---|---|
| Compose 프로젝트 | `infrastructure-langfuse` |
| 상시 서비스 | `lf-web`, `lf-worker`, `lf-postgres`, `lf-clickhouse`, `lf-redis`, `lf-minio` |
| 외부 공개 | Web/API `0.0.0.0:16320 → 3000` 한 포트 |
| 저장소/Worker | 전용 internal 네트워크. 호스트 포트 공개 없음 |
| Web 연결 | 전용 ingress + private 네트워크. 기존 `ops-infra`나 플랫폼 네트워크에는 연결하지 않음 |
| 기본 상태 경로 | `~/.local/state/agent-project-langfuse/` — 0700, 설정/키 파일 0600 |
| 실제 데이터 | 위 경로의 `data/postgres`, `data/clickhouse`, `data/clickhouse-logs`, `data/redis`, `data/minio` bind mount |
| 메모리 상한 | PostgreSQL 2G / ClickHouse 8G / Redis 1G / MinIO 2G / Web 4G / Worker 4G. 합계 21GiB, 예약량 아님 |
| 로그 | Docker json-file 서비스별 10MB × 3개. ClickHouse 파일 로그와 전체 저장소 용량의 quota는 아님 |
| 버전 | `images.lock.json`의 6개 immutable manifest digest. up 재실행 시 이동 태그 갱신 없음 |

일반 사용자·로컬 rootful Linux Docker·Compose 2.30 이상을 요구한다. 기동 전 CPU 4개·가용 RAM 24GiB, Docker 디스크 여유 20GiB, 데이터 파일시스템 여유 50GiB를 검사한다. 이전 Docker 파일시스템 사용률이 97%였으므로 데이터는 **Docker 저장소와 다른 파일시스템**, 프로젝트 소스 밖에 둔다. 공간 부족 시 자동 prune/삭제/기존 서비스 정지는 하지 않는다. 실제 이미지 다운로드와 신규 컨테이너 자원 사용은 발생한다.

서비스 기본 구성은 고정 Langfuse 릴리스의 공식 Compose를 참고했다. PostgreSQL 17.11 / ClickHouse 25.12.11.4 / Redis 7.4.11 이미지와 공식 Compose의 Chainguard MinIO 이미지를 digest로 고정했다. MinIO는 이동 태그 이름 대신 2026-09-26 확인한 digest만 사용한다. 공식 구성처럼 시작 전에 전용 `langfuse` 버킷 디렉터리를 준비한다. 새 ClickHouse 저장 디렉터리 두 곳의 소유권만 같은 고정 이미지의 일회성 컨테이너에서 UID/GID 101로 설정한다. 재귀 chown이나 기존 다른 DB 접근은 하지 않는다.

초기 비밀번호·API 키·DB 비밀은 headless 재기동을 위해 상태 폴더에 비공개로 보존한다. 화면 출력·명령 인자에는 비밀을 넣지 않는다. `compose config`, `docker inspect`, 환경 파일 원문에는 비밀이 포함될 수 있으므로 공유 로그로 올리지 않는다. 프로젝트용 초기 키는 합성 검사 용도이며 후속 Agent 전송/ops 조회 자격 증명 분리는 다음 단계다. Docker 관리자 권한까지 비밀을 감추는 구성이 아니다.

## 합성 추적 계약

- `POST /api/public/otel/v1/traces`, OTLP HTTP/JSON, `x-langfuse-ingestion-version: 4`.
- `GET /api/public/v2/observations`: trace ID·고정 시간 범위로 조회. 이전 API의 지연 집계에 의존하지 않음.
- 하나의 root + 성공/의도한 오류 자식, 총 3개 span. 부모 ID, 프로젝트, 이름, 상태/오류 문구, 시간, 환경, 메타데이터와 빈 input/output을 검증한다.
- 전송 전에 `trace-check.json`에 정확한 body·ID·시간·hash를 저장한다. 반복 `run`은 먼저 조회한다. 이미 확인됐으면 재전송하지 않는다. 전송 응답이 유실됐고 아직 조회되지 않을 때만 동일 본문/ID로 최대 3회까지 명시적 재실행을 허용한다.
- HTTP 수락 뒤 처리 지연은 `pending`/exit 2다. 이후 `check`로 조회한다. `check`는 POST·로컬 기록 변경·추론을 하지 않는다. 부분 거절이나 잘못된 부모/시간을 성공으로 처리하지 않는다.
- 합성 데이터는 Agent 실행 기록이 아니며 과거 AgentRun을 다시 실행하지 않는다. metadata only라는 이번 검사는 합성 payload의 범위이며 아직 없는 Agent 계측의 마스킹을 대신 검증한 것이 아니다.

## 재개와 점검

이미지 다운로드/부팅 중단 시 상태 폴더를 지우지 않고 같은 `--langfuse-only` 명령으로 재개한다. 저장 경로·소유권·고정 이미지·원래 디렉터리 inode가 바뀌면 빈 저장소를 자동 생성하거나 인수하지 않고 중단한다. 일부 환경 파일 쓰기만 중단됐다면 기존 설정에 기록된 같은 비밀로 누락된 파일만 복구한다. 변경된 파일은 자동 덮어쓰지 않는다.

```bash
python3 -B infra/langfuse/ops/manage.py status
python3 -B infra/langfuse/ops/manage.py check --wait-seconds 120
python3 -B infra/langfuse/ops/trace_check.py check --wait-seconds 120
```

`status`는 상태만 읽고 `check`는 Docker Healthy·API readiness·실제 버전·인증된 프로젝트 연결까지 확인한다. 추적 전달 증거는 `trace_check.py`가 별도로 판정한다. 실패 시 `manage.py diagnose`의 비밀을 가린 진단 결과를 공유한다. 설정/키/환경 파일은 공유하지 않는다. Docker 원문 오류에 비밀이 있을 수 있어 CLI는 요약을 출력한다.

의도적으로 Langfuse만 중지할 때:

```bash
python3 -B infra/langfuse/ops/manage.py stop
```

소유권을 확인한 해당 6개 컨테이너만 중지하며 데이터·컨테이너·네트워크를 삭제하지 않는다. 시작은 `--langfuse-only`다. 재시작 보존 인수나 백업 복구 검증을 자동으로 완료 처리하지 않는다. 커스텀 상태 경로를 쓰는 경우 `manage.py`/`trace_check.py` 모든 명령에 같은 `--state-dir`을 지정한다. 공통 up 진입점은 기본 경로용이다.

## 보관 정책과 남은 범위

고정 v4.46.0 소스에서 프로젝트 자동 보관기간은 `data-retention` entitlement를 요구한다. 이번 OSS 구성에 라이선스를 추가하지 않았고 보관기간이 적용된 것처럼 환경변수를 설정하지 않는다. readiness 결과의 실제 `retention_days`가 null/0이면 자동 삭제 설정 없음이다. **현재는 합성 trace 1개만 검사하며 본격 Agent 수집 전에 보관·용량 정책을 확정해야 한다.** 파일시스템 여유 검사와 Redis maxmemory/noeviction은 전체 추적 데이터의 하드 quota가 아니다.

별도 남은 작업: 고정 추적의 재시작 보존, 저장소별 일관된 백업·복원, Agent 계측/전송 장애 격리, 전송·조회 키 분리, ops API/화면, 미디어·배치 export, 사용자 화면 확인. 기존 GPU/Prometheus/Agent/n8n 인수는 그대로 유지하며 여기서 다시 실행할 필요 없다.

## 로컬 검사

```bash
python3 -B -m unittest discover -s infra/langfuse/tests -v
```

최초 구현 22개 Python 검사: 실제 로컬 HTTP fixture를 통한 OTLP JSON/조회·응답 유실·같은 본문 재사용·부분 거절·읽기 전용 재조회, 상태/환경/저장 연결·비밀 보존, infra 선택기와 기동 명령 범위. 별도로 실제 Compose 2.39.4 해석, 특수문자 비밀번호 raw env 해석, Python 3.9 문법을 확인했다. 로컬에는 Docker daemon이 없어 6개 실제 컨테이너 시작이나 제품 통합 검사를 수행하지 않았다. 이후 사용자 서버가 수행한 복구·합성 run/check 로그로 기동/수집 인수를 별도 기록했다.

근거: [고정 릴리스 Compose](https://github.com/langfuse/langfuse/blob/v4.46.0/docker-compose.yml), [초기화 코드](https://github.com/langfuse/langfuse/blob/v4.46.0/web/src/initialize.ts), [v2 observations 계약](https://github.com/langfuse/langfuse/blob/v4.46.0/fern/apis/server/definition/observations.yml), [OTLP 전송](https://langfuse.com/integrations/native/opentelemetry). 역할 경계는 [플랫폼 설계](../../docs/ops-platform/langfuse.md)를 따른다.
