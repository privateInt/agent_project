# Agent Project

개발 준비: [Codex 규칙·구성·템플릿](docs/codex/README.md) · [프로젝트별 규칙](docs/codex/project.md) · [준비와 첫 작업](docs/codex/work-plan.md). 공통 기준을 이 저장소에도 포함하며 본격 기능 개발과 무인 실행은 실행 환경 확인 후 시작합니다.


Model·Data·Knowledge·Agent·Ops를 독립 플랫폼으로 구성한 AI 개발·실행 프로젝트다. 현재 코드에는 모델 관리·학습·배포부터 자료 수집·정제·검색, 문서 질의응답 Agent와 운영 관측까지 연결할 기능이 있다. **핵심 기능 구현과 제한된 서버 검증은 진행됐지만, 전체 제품 인수와 전 플랫폼 재해복구는 완료되지 않았다.**

2026-10-07 최신 첨부 `agent_project(5).tar`의 코드·문서·완료 근거를 대조했다. 현재 호스트/이미지의 일치·실행 상태는 이 업로드만으로 보장하지 않는다.

## 먼저 읽을 문서

| 목적 | 문서 |
|---|---|
| 완료 범위와 미완료 상태 | [현재 상태](docs/status.md) |
| 코드 근거와 검증 한계 | [코드 분석 보고서](docs/reviews/code-review-20261001.md) |
| 앞으로 진행할 순서 | [로드맵](docs/roadmap.md) |
| 문서 QA 완성 계획 | [작업계획서](docs/plans/document-qa-work-plan-20261002.md) |
| 다음 세션/Codex 시작점 | [인수인계](docs/HANDOFF_NEXT_SESSION.md), [작업 규칙](AGENTS.md) |
| 채택한 확장 10개와 실행 순서 | [확장 계획](docs/plans/platform-expansion-work-plan-20261012.md) |
| 설계와 개발 기준 | [아키텍처](docs/architecture.md), [설계 원칙](docs/design-principles.md), [개발 안내](docs/development.md) |
| API와 운영 절차 | [문서 목차](docs/README.md) |

## 소스 구조

소스는 `src/platform/{agent,data,knowledge,model}`, `src/ops/{platform,tools,candidates}`, `src/apps`, `src/infra`에 모았다. 문서는 `docs`에 둔다. [경로·실행 안내](docs/architecture/source-layout.md)를 따른다.

## 플랫폼 역할

| 플랫폼 | 소유 기능 | 안내 |
|---|---|---|
| Model | 모델·데이터셋·학습·배포·추론 | [구조](docs/architecture.md#model-서비스와-데이터-소유권) |
| Data | 원본 수집·업로드·정제·canonical | [Data](docs/data-platform/README.md) |
| Knowledge | 청킹·색인·버전·벡터 검색·선택적 rerank | [Knowledge](docs/knowledge-platform/README.md) |
| Agent | Connection·Tool·MCP·Agent 버전과 실행 | [Agent](docs/agent-platform/README.md) |
| Ops | 플랫폼·자원·추적·경보 조회와 운영 관측 | [Ops](docs/ops-platform/README.md) |
| infra | 공용 저장소·관측 도구·n8n 기동과 설정 | [로컬 운영](docs/operations/local.md) |

각 플랫폼은 공식 API로 연결한다. n8n은 상위 업무 흐름, LangGraph는 Agent 내부의 고정 질의응답 실행을 담당한다. 현재 Agent가 임의 그래프 편집기를 제공하는 것은 아니다.

## 현재 작업

문서 QA 첫 TXT의 실제 GPU 색인·검색 Tool·QA Agent smoke/발행·head revision1과 LAN 화면/질문 접수를 확인했다. 현재 PC 코드 질문과 인용 강화 초안은 MISSING_REFERENCE로 실패했으며 첫 답변/출처·품질 인수는 남았다. 최신 범위와 근거는 [상태표](docs/status.md), 실제 환경/첫 작업은 [인수인계](docs/HANDOFF_NEXT_SESSION.md)를 따른다.

학습데이터 Data 이전·Data 일정/Airflow·infra·기본 MCP·학습 형식/RAFT·HWP·상용 API·세션/기억·문서 Tool·CI/CD의 10개 확장을 [계획](docs/plans/platform-expansion-work-plan-20261012.md)에 반영했다. Codex는 [AGENTS.md](AGENTS.md)에 따라 소스 구조 정리의 검증을 완료한 뒤 F0a부터 작은 단계로 진행한다. n8n/Langfuse는 기존 구성을 재사용하고 LightRAG/Neo4j는 평가 실험으로 분리한다. 계획 문서 적용은 신규 infra 설치나 기능 구현 완료를 뜻하지 않는다. Ops 운영 백업/복원·재부팅·Kubernetes 보류는 유지한다.
