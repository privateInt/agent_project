# 코드 기준 프로젝트 분석

2026-10-01 업로드한 `agent_project(4).tar`를 기준으로 Model·Data·Knowledge·Agent·Ops의 주요 API, 실제 처리·저장·실행 경로, Compose, 테스트와 보존된 서버 결과를 대조했다. **프로젝트는 핵심 기능 연결을 구현한 단계이며, 현재 진행선은 운영 안정화와 사용자 인수다. 전체 기능이 설계만 된 상태는 아니지만, 제품 인수와 전체 복구 완료로 보기도 어렵다.**

## 분석 방법과 판정 범위

문서의 완료 문구보다 실행 코드를 먼저 읽었다. Gateway에 경로가 있다는 것만으로 구현 완료로 보지 않고 도메인 처리, Job/runtime, 상태 전이와 결과 검사를 연결해 확인했다. 과거 서버 결과는 업로드에 포함된 JSON/검증 문서를 사용했다. 운영 서버에 접속하거나 현재 컨테이너 이미지와 소스를 비교하지 않았다.

실행 코드에 속하지 않는 이전 패치 백업은 현재 구현 판정에서 제외했다. 기본 구조의 일반 파일에는 Python 415개·Go 540개·JS/CJS 25개·Markdown 241개가 있었다. 생성된 module/protobuf는 Docker 빌드에서 만드는 구조여서 호스트 `go.mod` 부재를 결함으로 분류하지 않았다. 원본 archive hash와 새 검사 상세는 [검증 JSON](../testing/source-review-20261001.json)에 보존했다.

## 코드로 확인한 기능

| 영역 | 구현 확인 | 직접 근거 |
|---|---|---|
| Model 관리 | 모델/버전·HF import·데이터셋·이름 변경·삭제·목록 | [REST routes](../../model_platform/gateway/app/routes.go), [Management](../../model_platform/management/app/), [datasets](../../model_platform/training/app/datasets.go) |
| Model 실행 기반 | 멱등 접수·submission·Operation/Outbox·Kafka·Dispatcher·실행 승인·Reconciler·GC | [creation](../../model_platform/training/app/creation.go), [Operation](../../model_platform/operation/app/), [registry](../../model_platform/dispatcher/registry/registry.go), [GC](../../model_platform/gc/) |
| Model 학습 | Axolotl 설정·단일 GPU/로컬 DDP 요청·취소/로그·output 복구·checkpoint·평가 | [spec](../../model_platform/training/app/spec.go), [Axolotl](../../model_platform/jobs/training/axolotl/), [training](../../model_platform/training/app/) |
| Model 추론 | snapshot/adapter vLLM·지정 OCR/embedding/reranker profile·chat/중지 | [배포 생성](../../model_platform/deployment/app/creation.go), [runtime](../../model_platform/deployment/runtime/), [REST](../../model_platform/gateway/app/routes.go) |
| Data 수집 | HTTP·Chromium·첨부·업로드·원본/그룹·정제 요청 | [REST](../../data_platform/gateway/app/server.go), [collection jobs](../../data_platform/jobs/collection/), [groups](../../data_platform/gateway/app/groups.go) |
| Data 정제 | text/Markdown/HTML/DOCX/XLSX/PDF/PNG/JPEG 처리·OCR·품질 상태·좌표/부분 보존 | [parser](../../data_platform/jobs/refinement/parse/parser.py), [OCR](../../data_platform/jobs/refinement/parse/ocr.py), [refinement job](../../data_platform/jobs/refinement/parse/main.go) |
| Knowledge 청킹 | recursive/structure 두 전략·버전 고정·표/코드 구조 분할·출처 spans | [registry](../../knowledge_platform/internal/chunking/registry.go), [engine](../../knowledge_platform/internal/chunking/engine.go), [structured](../../knowledge_platform/internal/chunking/structured.go) |
| Knowledge 색인 | Model embedding·HNSW 설정·배치/진행·active 버전·삭제/참조 | [indexes](../../knowledge_platform/service/app/indexes.go), [worker](../../knowledge_platform/service/app/index_worker.go), [management](../../knowledge_platform/service/app/index_management.go) |
| Knowledge 검색 | 고정 벡터/공간/버전·필터·후보 검증·선택적 Model rerank | [search](../../knowledge_platform/service/app/search.go), [OpenSearch query](../../knowledge_platform/internal/searchstore/search.go), [rerank](../../knowledge_platform/service/app/rerank.go) |
| Knowledge 평가 | 고정 자료·정답 spans·variant·hit/recall/MRR/nDCG·후보 진단 | [evaluation](../../knowledge_platform/evaluation/), [metrics](../../knowledge_platform/evaluation/metrics.py) |
| Agent Tool | OpenAPI 3.0/3.1 지원 부분집합·직접 등록·Connection/Tool 버전·시험/발행·MCP | [OpenAPI parser](../../agent_platform/internal/apispec/openapi.go), [Tool routes](../../agent_platform/gateway/app/tools.go), [MCP](../../agent_platform/mcp/app/server.go) |
| Agent 검색 | 질문→Model embedding→Knowledge 검색·버전/profile 검증 | [knowledge tools](../../agent_platform/runtime/agent_runtime/knowledge_tools.py), [Service](../../agent_platform/service/app/knowledge.go) |
| Agent 실행 | 고정 QA·5단계·인용 형식·실행/오류 기록·n8n link | [definition](../../agent_platform/internal/agenting/model.go), [runtime](../../agent_platform/runtime/agent_runtime/agents.py), [workflow link](../../agent_platform/internal/agenting/workflows.go) |
| Ops 조회 | 세션·통합 UI·4개 플랫폼·자원·trace·경보 | [server](../../ops_platform/server.py), [service](../../ops_platform/service.py), [client](../../ops_platform/client.py), [alerts](../../ops_platform/alerts.py) |
| Ops 자동갱신 | Compose/Kubernetes adapter·설정 refresh·worker·systemd/boot·복구 | [ops modules](../../ops_platform/ops/), [worker](../../ops_platform/ops/refresh_worker.py), [recovery](../../ops_tools/recover_boot_worker.py) |
| Ops 백업 | 준비·metadata plan·generation/restore 코어·격리 native 인수 | [plan](../../ops_tools/backup_plan.py), [generation](../../ops_candidates/backup_generation_20261001/generation.py), [native acceptance](../../ops_tools/backup_generation_acceptance.py) |

## 완료 범위를 좁혀야 하는 지점

### 운영 백업은 실행 직전까지 모두 끝난 상태가 아니다

`backup_plan.py`는 `plan`만 허용하고 결과에 `production_execution_adapter_available=False`, `backup_ready=False`를 명시한다. generation 코어도 운영 CLI를 제공하지 않는다. native acceptance는 격리 fixture·새 복원 디렉터리·test user unit에서 암호화 수집과 파일시스템 복원을 수행한다. 코드상 image import/application restore 판정은 false다.

[16/16 서버 결과](../testing/ops-backup-generation-native-server-20261001.json)는 이 격리 범위의 성공이다. 운영 목적지/새 키 준비와 worker 복구는 이후 별도 성공 기록이 있지만, 마지막 [운영 plan 결과](../history/ops-evidence-catalog.md#ops-backup-plan-snapshot-observed-20261001)는 `BACKUP_GENERATION_DISCOVERY_CHANGED`다. 업로드 코드에는 마운트 목록 정렬과 변경 필드 진단이 있다. 해당 수정 후 실제 plan 결과는 없으므로 서버 해결 완료로 판정하지 않는다.

다음 구현은 검토 계약과 유지보수·중단·용량/시간·실패 복구를 연결하는 운영 capture 실행기다. 첫 운영 세대와 그 세대의 실제 DB/image/application 복원도 남는다.

### 현재 Ops 백업은 프로젝트 전체 데이터 백업과 다르다

[preflight](../../infra/observability/ops/backup_preflight.py)와 [plan source 연결](../../ops_tools/backup_plan.py)은 관측 저장소·Ops·Langfuse·관련 코드/설정/worker를 다룬다. 별도 [Model common](../../model_platform/ops/common.py)의 백업 DB 목록은 Model 4개 DB다. Data/Knowledge/Agent/n8n 전체의 일관된 시점 백업과 복구 순서를 완료했다는 코드/결과는 없다.

따라서 “백업 완료”라는 하나의 문구 대신 관측 백업, Model 백업, 전체 플랫폼 복구를 구분해야 한다. [통합 범위 안내](../operations/backup-restore.md)와 [Model 전용 절차](../operations/model-backup-restore.md)로 문서를 분리했다.

### Agent는 고정 QA 기반이며 의미 품질 인수는 남아 있다

[Definition 검증](../../agent_platform/internal/agenting/model.go)은 `knowledge_qa_v1`/version 1만 받으며, runtime은 `validate → search → context → generate → finalize`를 고정한다. n8n 링크는 workflow ID와 Agent 버전을 연결하는 기능이다. 임의 내부 그래프 편집이나 자율 Tool 선택을 구현한 것은 아니다.

[A09 결과](../../agent_platform/tests/results/a09-server-20260925.json)에서 개선 prompt 버전은 답변 전달·동일 요청 재사용·재시작 보존에 성공했다. 같은 자료에는 질문 문구를 5회 반복하고 실질적 설명이 없었다는 관찰도 있다. 코드의 인용 문법 검사는 의미 정확성/유용성 검사가 아니므로 둘은 모순되지 않는다. 별도 [구버전 추적 실행](../../agent_platform/tests/results/o02-tracing-server-20260927.json)의 `MISSING_REFERENCE`는 그 실행의 실패로 유지한다.

현재 보류한 화면/Flow 편집·의미 품질 인수를 마무리해야 MVP 전체를 완료로 볼 수 있다. API schema 입력 부담을 줄이는 범용 서비스 카탈로그/자동완성도 후속이다.

### Knowledge는 실제 평가를 수행했지만 hybrid 검색은 없다

OpenSearch [searchBody](../../knowledge_platform/internal/searchstore/search.go)는 kNN 질의를 구성한다. BM25·RRF 경로는 현재 코드에 없다. query embedding은 Agent/호출자가 Model API에서 먼저 얻는다. rerank는 Knowledge가 Model API로 수행한다.

[12문항 비교 결과](../../knowledge_platform/tests/results/evaluation-candidate-diagnostics-20260921.json)는 새 구조 청킹+rerank에서 해당 자료의 hit@5 1.0, MRR@5 0.875를 기록한다. 이는 제한된 비교 결과이며 일반 품질 보증이 아니다. recursive ON 원본 후보 불일치 1건은 원인이 미확정이다. “평가 미실행”이나 “MCP 설계가 다음 작업”이라는 초기 문구는 현재 상태와 맞지 않아 수정했다.

### Model 확장 기능과 실제 검증 범위를 구분해야 한다

[학습 creation](../../model_platform/training/app/creation.go)은 한 호스트 1~16 GPU를 허용하고 다중 GPU는 DDP로 정규화한다. remote 노드는 별도 설정 검증을 하고 현재 경로는 정확한 GPU ID 하나를 요구한다. adapter를 source로 한 추가 학습은 명시적으로 거절한다. checkpoint/evaluation은 로컬 단일 GPU 등 추가 제한이 있다.

[spec](../../model_platform/training/app/spec.go)과 Axolotl 변환 코드에는 FSDP/DeepSpeed를 외부 요청 계약으로 지원하는 구현이 없다. Axolotl 자체의 가능성과 이 플랫폼의 제공 기능을 구분한다. LiteLLM·ZenML 통합도 현재 코드에 없다. 실제 단일 GPU Gemma 학습/서빙 기록을 DDP·원격·모든 모델 검증으로 확대하지 않는다.

### Data 처리가 성공해도 인식 품질이 완료되는 것은 아니다

parser/OCR는 품질 상태와 원본 위치·부분 텍스트를 보존한다. [Data 상태](../data-platform/status.md)에 남은 최종 자동 영역 실험은 품질 통과 0/9·timeout 2건이다. 과거 다른 합성 OCR 성공 기록은 유효하지만 이 실패를 지우지 않는다. 사용자 결정대로 일부 오류를 허용하며 활용하고, PDF 제목 추정·어려운 표/그림·ZIP은 별도 후속으로 남긴다.

## 앞으로의 순서

1. 최신 Ops backup plan의 수정 후 서버 결과를 확인한다.
2. 검토된 운영 capture/explicit recovery 실행기를 한 단계로 구현·검증한다.
3. 구체화한 운영 범위에서 첫 관측 백업과 별도 환경 애플리케이션 복원을 확인한다.
4. Agent의 실제 사용 흐름과 서비스 API 등록 부담을 정리한다.
5. 보류 재개를 결정하면 대표 질문으로 답변 의미 품질을 확인한다.
6. 전체 플랫폼 복구 범위와 지원 환경 확대를 별도 단계로 진행한다.

실제 재부팅·Kubernetes·대규모 확장을 현재 단계의 필수 조건으로 추가하지 않는다. 상세 완료선은 [로드맵](../roadmap.md)을 따른다.

## 이번 새 검증

| 범위 | 실행 결과 | 의미 |
|---|---:|---|
| Model Python 선택 검사 | 53 통과 | 백업 대역·학습 설정/체크포인트/평가 도구 |
| Data Python 선택 검사 | 81 통과 | parser·좌표·모의 OCR·요청/업로드 도구 |
| Knowledge Python 검사 | 76 통과 | 평가/색인/검색 도구·로컬 HTTP 대역 |
| Agent Python 선택 검사 | 60 통과 | 진단·prompt trial·검색 Tool 준비·인수/복구 도구 |
| Ops Python 선택 검사 | 44 통과·45 건너뜀 | plan/복구 로직. pinned age 도구 부재로 45개 실행 안 됨 |
| Node UI 계약 | 8개 스크립트 통과 | DOM/API 대역. 실제 브라우저 렌더링 인수 아님 |
| 정적 형식 | Python AST 415·JSON 266·JS/CJS 문법 25 정상 | 실행/통합 성공을 대신하지 않음 |

합계는 **314 통과·45 건너뜀·실패 0**이다. 환경에 Go/Docker/protoc와 Agent runtime 의존성이 없어 Go 빌드·DB/GPU·전체 runtime 검사·native age 인수를 실행하지 않았다. 설치/다운로드나 운영 접근을 추가하지 않았다. 날짜별 과거 테스트 수를 이번 통과 수에 합산하지 않았다.

이번 수정은 문서와 이번 검증 요약에 한정된다. 런타임 소스·Compose·설정·기존 결과 JSON·후보/source pin은 유지하고, 적용 패키지는 변경 전 해시 검사와 백업 후 문서만 반영한다. 문서 링크/패키지 검증 결과는 [정리 내역](../history/documentation-refresh-20261001.md)에 둔다.
