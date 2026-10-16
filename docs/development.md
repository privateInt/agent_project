# 개발 안내

2026-10-16: 제출 전 저널은 `codex_unattended_intent_journal.py`의 initialize/reserve/read/claim 네 API만 사용한다. caller-owned 전용 파일 SQLite 연결을 빌리고 빈 DB의 명시 초기화만 허용한다. 기존 DB migration·연결 생성/종료·실제 queue IO는 없다. DB claim은 실행 권한이 아니며 UNKNOWN은 재claim하지 않는다. [새 검사 근거](testing/codex-unattended-intent-observed-20261016.json)는 최초 실패와 해당 method만 재검증한 결과를 구분한다.

2026-10-16: [순수 admission 선택기](../src/ops/tools/codex_unattended_admission.py)는 외부 config/snapshot/state와 기존 gate의 두 순수 함수를 받아 후보만 반환한다. [합성 검사·변이·이식성 근거](testing/codex-unattended-admission-observed-20261016.json)를 실제 queue 실행과 구분한다. 후보는 실행·설정 변경 권한이 아니며 설치된 gate를 수정하지 않는다.

2026-10-13 추가: 현재 개발 사본의 첫 작업은 src 구조·Ops 통합과 경로 검증이며 다음 기능 작업은 F0a다. [소스 구조](architecture/source-layout.md)를 따른다. 아래 날짜별 인수 상태와 운영 자산은 유지한다.

Codex 작업은 [AGENTS.md](../AGENTS.md), [최신 인수인계](HANDOFF_NEXT_SESSION.md), [확장 계획](plans/platform-expansion-work-plan-20261012.md)의 첫 미완료 작은 단위부터 수행한다. 사용자 서버·대역 검사·품질 인수와 계획/실제 구현을 구분한다. 계획 파일만 적용한 상태를 CI/CD나 상시 Agent 실행 완료로 표시하지 않는다.

## 공통 개발 기준

[자동화와 적응성](design-principles.md)을 최우선으로 검토한다. 새 기능의 필수 사용자 입력과 환경 의존 주소/이름/경로를 먼저 식별하고, 자동 발견·기능 확인·설정 생성·변경 적응으로 줄인다. 공통 기능에 환경별 고정값을 추가하기 전에 환경 adapter와 소유 플랫폼 API로 해결할 수 있는지 확인한다. 명시적 재정의와 기존 설정/키의 재사용을 지원한다.

완료 근거에는 최초 수동 입력 수, IP/포트·경로·namespace 변경 후 수동 수정 수, 대상 0개/1개/여러 개와 미지원/권한 부족 처리, 재실행의 중복 생성 여부를 해당 범위에서 기록한다. 설정 가능·자동 발견·자동 갱신·실제 환경 인수는 별도 상태로 표시한다. 검증 시나리오의 공통 기준은 위 문서 한 곳에서 관리한다.

## 플랫폼별 코드 시작점

| 영역 | 먼저 읽을 코드 |
|---|---|
| Model | `src/platform/model/gateway/app/routes.go` → 각 도메인의 `app/creation.go` → Operation/Dispatcher/Job |
| Data | `src/platform/data/gateway/app/server.go` → collection/refinement → `jobs/refinement/parse/parser.py`와 `ocr.py` |
| Knowledge | `gateway/app/indexing.go` → `service/app/indexes.go`, `search.go` → `internal/chunking`, `internal/searchstore` |
| Agent | `gateway/app/tools.go`, `agents.go` → `service/app` → `runtime/agent_runtime/agents.py`, `knowledge_tools.py` |
| Ops | `src/ops/platform/server.py`, `service.py` → 조회 모듈 → `ops/refresh_worker.py`; 백업 plan/실행기는 `src/ops/tools/backup_plan.py`, `backup_execute.py` (운영 measure 차단/보류) |
| 문서 QA 연결 | `src/apps/document_qa/api.py` → uploads/workflows/refinement/indexing → `qa_agent.py`/`question_head.py`/`web/questions.js`. native 진단은 `src/platform/agent/ops/diagnose_answer.py`, 현재 F0a 인용 실패는 [인수인계](HANDOFF_NEXT_SESSION.md) |

한 번에 한 단계의 산출물과 완료선을 정한다. 새 문서 상태는 `docs/status.md`와 `docs/roadmap.md`에 반영하고 진행 문단을 여러 README에 복제하지 않는다. 아래 Model 세부 지침과 각 플랫폼 계약은 유지한다.

고정 fixture의 설치 pin·전용 상태/기존 SQLite 경로 검증은
[fixture 설정 로더](codex/github-fixture-config.md)를 따른다. 실제 소유자 승인값과
운영 설치 인수는 별도이며 기존 DB를 자동 초기화하거나 migration하지 않는다.

## 코드 읽기 순서

| 순서 | 파일 / 디렉터리 | 확인할 내용 |
|---|---|---|
| 1 | [contracts](../src/platform/model/contracts) | REST 뒤의 gRPC 계약과 리소스 표현 |
| 2 | [Gateway routes](../src/platform/model/gateway/app/routes.go) | JSON 입력, gRPC 요청, 응답 변환 |
| 3 | [Management](../src/platform/model/management/app), [Training](../src/platform/model/training/app), [Deployment](../src/platform/model/deployment/app) | creation.go의 도메인 검증·트랜잭션, 예약 Operation 전달 |
| 4 | [Operation app](../src/platform/model/operation/app) | 아래 책임 분리 표 |
| 5 | [Outbox](../src/platform/model/operation/outbox/worker.go), [Kafka publisher](../src/platform/model/operation/messaging/kafka.go) | DB 이벤트 발행과 재시도 |
| 6 | [Delivery](../src/platform/model/dispatcher/app/delivery.go), [Registry](../src/platform/model/dispatcher/registry/registry.go), [Docker Executor](../src/platform/model/dispatcher/executor/docker.go) | Claim, routing, 실행, offset 처리 |
| 7 | [operationreport](../src/platform/model/internal/operationreport/client.go), [jobs](../src/platform/model/jobs) | BeginExecution, 작업과 lifecycle 보고 |
| 8 | [Reconciler](../src/platform/model/reconciler) | 관찰 증거, 상태 보정, 실행 전 복구 |
| 9 | [GC](../src/platform/model/gc) | policy의 수집 조건, worker의 재조회·삭제 순서, archive의 로그 manifest와 readback |

## Operation 파일 책임

| 파일 | 책임 |
|---|---|
| [service.go](../src/platform/model/operation/app/service.go) | Service 구성, CreateOperation/EnsureOperation/GetOperation RPC |
| [schema.go](../src/platform/model/operation/app/schema.go) | operations와 Outbox 스키마 초기화 |
| [repository.go](../src/platform/model/operation/app/repository.go) | 공통 SELECT, row 변환, 조회·잠금·commit readback |
| [lifecycle.go](../src/platform/model/operation/app/lifecycle.go) | MarkRunning, UpdatePhase, 성공·실패 보고, fenced transition |
| [transition_policy.go](../src/platform/model/operation/app/transition_policy.go) | 허용 전이와 terminal 중복 판단 |
| [dispatch.go](../src/platform/model/operation/app/dispatch.go) | Claim, BeginExecution, RecoverDispatch, FailDispatch |
| [reconciliation.go](../src/platform/model/operation/app/reconciliation.go) | 후보 조회와 관찰 결과 CAS 반영 |

공통 SELECT의 컬럼 순서와 `scanOperation` 순서는 한 쌍이다. 조회 필드를 바꿀 때 둘을 함께 수정하고 DB 통합 테스트를 실행한다. 이 분리는 같은 Go package 안의 책임 분리이며 새 서비스나 저장소 계층을 추가한 것이 아니다.

## 생성 경계의 공통 코드

각 도메인 `app/creation.go`는 정규화·검증과 리소스 저장을 담당한다. [idempotency](../src/platform/model/internal/idempotency/requests.go)는 같은 트랜잭션 안의 키 경합·응답 재현을, [submission](../src/platform/model/internal/submission/submission.go)은 예약 Operation ID의 내구성 있는 전달을 담당한다. Job 실행 명세와 credential은 도메인이 계속 소유한다.

입력 검증과 기본값 정규화 뒤 fingerprint를 만들고, 최초 요청만 외부 의존성을 확인한다. 재현 요청은 과거의 수락 응답을 반환하므로 원본 모델이 나중에 조회되지 않아도 새 작업을 만들지 않는다. 생성 변경 시 [REST·gRPC·DB 테스트](testing/request-creation.md)를 포함한다.

## Deployment 서빙 파일 책임

| 위치 | 책임 |
|---|---|
| [creation.go](../src/platform/model/deployment/app/creation.go) | demo 호환성, GPU 검증, 요청 멱등성과 예약 Operation |
| [serving_source.go](../src/platform/model/deployment/app/serving_source.go) | snapshot/adapter 검증, 정확한 base 연결과 source 고정 |
| [serving.go](../src/platform/model/deployment/app/serving.go) | 스키마, 실행 승인 RPC, Stop 의도 저장과 상태 표현 |
| [serving_controller.go](../src/platform/model/deployment/app/serving_controller.go) | DB 잠금, 준비·관찰·중지, 완료 결과 보존 |
| [runtime](../src/platform/model/deployment/runtime) | 서빙 명세, manifest·파일 검증, Docker 명령과 health 확인 |
| [adapter_artifacts.go](../src/platform/model/deployment/runtime/adapter_artifacts.go) | root 추론 파일 선택과 manifest 검증 |
| [adapter_weights.go](../src/platform/model/deployment/runtime/adapter_weights.go) | safetensors 검사, 실행할 base와 embedding 비교, LoRA 서빙용 사본·receipt |
| [adapter_base.go](../src/platform/model/deployment/runtime/adapter_base.go) | format 2의 별도 base 사본, 저장된 embedding·출력층 보존, 캐시 검증 |
| [adapter_saved_tokenizer.go](../src/platform/model/deployment/runtime/adapter_saved_tokenizer.go) | Gemma의 저장 tokenizer 선택, special 변경 범위·토큰 ID 검사 |
| [adapter_tokenizer.go](../src/platform/model/deployment/runtime/adapter_tokenizer.go) | tokenizer 비교, 저장된 채팅 템플릿 선택 |
| [serving Job](../src/platform/model/jobs/deployment/apply/serving.go) | BeginExecution 후 StartServing과 Operation 보고 |

서빙 모델의 GPU는 Deployment 도메인에 남기고 시작 Job은 CPU 자원으로 전달한다. 원격 응답 유실을 바로 실패로 처리하거나 알려진 종료 런타임을 재시작하지 않는다. 상태·식별자 변경은 [서빙 DB 테스트](testing/serving.md)를 포함한다. adapter 변경은 [adapter 테스트](testing/adapter-serving.md)를 따른다. 원본 학습 파일을 수정하거나 검증 없이 embedding을 제외하지 않는다.

## Data 화면 코드 책임

| 파일 | 책임 |
|---|---|
| [data_ui.py](../src/platform/data/console/data_ui.py) | 목록·진행 상태 조회, 수집 결과 선택, 화면 조합 |
| [data_ui_actions.py](../src/platform/data/console/data_ui_actions.py) | 화면 이동, 명시적인 사용자 요청, 요청 보관·재전송 |
| [data_ui_documents.py](../src/platform/data/console/data_ui_documents.py) | 원본 미리보기, 정리 결과 표시·다운로드 |

문서 표시 → 요청 처리 방향으로만 의존하며 요청 모듈이 화면 조합 모듈을 다시 불러오지 않는다. 5초 조회 주기·terminal 뒤 조회 중단·POST 전 입력 보관·같은 키 재전송을 유지한다. [콘솔 검사](data-platform/testing-console.md)로 사용자 동작을 확인한다.

[Data Dockerfile](../src/platform/data/Dockerfile)은 전체 소스와 의존성을 가진 `builder`를 Go/DB 테스트용으로 유지한다. `build-operation` 등 8개 빌드 단계는 각각 해당 실행 파일 하나만 컴파일한다. HTTP·Chromium 수집은 같은 `build-crawl-job`을 사용한다. 소스 복사와 `go mod tidy`는 아직 공유하므로 소스 변경에 따른 캐시 무효화까지 서비스별로 분리된 것은 아니다. 기존 Compose target·이미지 이름·실행 사용자·자체 검사는 유지한다.

## 빌드와 수정 원칙

- Go module은 [Dockerfile](../src/platform/model/Dockerfile)의 `go-deps`, protobuf는 `go-contracts`에서 생성한다. `builder`는 전체 소스를 가진 테스트 환경이다. 호스트에 `go.mod/go.sum`을 추가하지 않는다. 서비스별 Compose·이미지·캐시 구조는 [빌드 안내](operations/build.md)를 따른다.
- 외부 API는 플랫폼 개념으로 표현한다. Axolotl 옵션 변환은 [config_adapter.py](../src/platform/model/jobs/training/axolotl/config_adapter.py)가 맡는다.
- 요청 수락, 외부 실행, 도메인 결과 저장, Operation 완료 보고의 순서를 유지한다. 성공 응답 전에 필요한 DB 오류를 전달한다.
- Job은 실행 승인 전 도메인 처리나 GPU 작업을 시작하면 안 된다. lease 판단의 `clock_timestamp()`와 PostgreSQL의 명시적 `::varchar` 타입을 보존한다.
- 수정한 책임과 연결되는 기존 테스트를 고른다. 단순 문서 변경에 GPU E2E를 반복할 필요는 없다. 실행 승인·트랜잭션 변경은 PostgreSQL 통합 테스트를 포함한다.
- 설명 변경은 이 문서 목차에 연결한다. 코드 옆 설명용 README, 별도 HANDOFF/STATUS 사본, 일회성 설치 문서를 늘리지 않는다.

검증 명령은 [테스트 안내](testing/README.md), tar 전달·삭제 규칙은 [운영 팁](operations/tips.md)에 둔다.


## 학습 출력 보존과 복구

[trainingoutput](../src/platform/model/internal/trainingoutput)은 완료 receipt·파일 검증·공유 잠금과 공통 publication 순서를 담당한다. [Training output controller](../src/platform/model/training/app/output.go)는 요청 멱등성·DB 복구 상태·원래 실행 증거·정리를 담당한다. 복구는 새 실행 Operation을 만들지 않고 원래 실패 이력을 보존한다. 조회 응답에 receipt의 실행 승인 값이나 로컬 경로를 노출하지 않는다. 변경 시 [출력 복구 검증](testing/training-output.md)을 사용한다.
