# 프로젝트 문서 목차

현재 상태·다음 작업·근거 문서를 분리한다. 날짜별 검증 기록은 당시 범위를 보존하며 최신 운영 상태를 대신하지 않는다.

## 시작점

| 읽을 목적 | 문서 |
|---|---|
| 현재 구현과 미완료 | [상태](status.md) |
| 최신 문서 검수/통합 | [2026-10-07 검수](reviews/documentation-review-20261007.md), [Ops 원문 목차](history/ops-evidence-catalog.md) |
| 코드에 근거한 종합 점검 | [2026-10-01 분석](reviews/code-review-20261001.md) |
| 다음 개발 순서 | [로드맵](roadmap.md) |
| 확장 10개·플랫폼 책임·Codex 단계 완료선 | [확장 계획](plans/platform-expansion-work-plan-20261012.md), [AGENTS.md](../AGENTS.md) |
| 최신 GPU/Tool/Agent/head 인수와 PC 인용 실패 | [2026-10-12 실제 관찰](testing/document-qa-live-observed-20261012.json) |
| 현재 문서 QA 완성 계획 | [작업계획서](plans/document-qa-work-plan-20261002.md) |
| 문서 QA 연결 계약과 검사 | [Q01 계약](document-qa/contracts.md), [서버 적용 결과](testing/document-qa-q01-server-observed-20261002.json) |
| 연결·모델 기본값과 준비 상태 | [Q02 안내](document-qa/readiness.md), [서버 확인](testing/document-qa-q02-server-observed-20261002.json) |
| 문서 업로드·작업 저장·중복 처리 | [Q03a 안내](document-qa/uploads.md), [서버 확인](testing/document-qa-q03a-server-observed-20261007.json) |
| 문서 준비 workflow 전달/receipt | [Q03b 안내](document-qa/workflows.md), [서버 확인](testing/document-qa-q03b-server-observed-20261007.json) |
| CPU 문서 정제 접수·결과 검증 | [Q04a 안내](document-qa/refinement.md), [서버 근거](testing/document-qa-q04a-server-observed-20261007.json) |
| n8n 자동 CPU 정제·단계 실행권 | [Q04b 안내](document-qa/refinement-workflow.md), [새 검사](testing/document-qa-q04b-offline-20261007.json) |
| TXT 실제 업로드·n8n·Data CPU 정제 인수 | [2026-10-07 서버 근거](testing/document-qa-cpu-live-observed-20261007.json), TXT CPU 구간 인수 통과 |
| 색인 접수·상태·활성 버전 보존 | [Q05a 안내](document-qa/indexing.md), [GPU 없는 검사](testing/document-qa-q05a-offline-20261007.json) · [서버 확인](testing/document-qa-q05a-server-observed-20261007.json) |
| n8n 색인 단계·보류 실행권 | [Q05b 안내](document-qa/indexing-workflow.md), [새48개/전체324개 검사](testing/document-qa-q05b-offline-20261007.json) · [실제 서버 등록/보류 callback](testing/document-qa-q05b-server-observed-20261007.json) |
| GPU 모델 준비 후 색인 설정 오류 복구 | [2026-10-12 표시 필드 호환 수정·재개 절차](document-qa/indexing-profile-fix.md), [개발 검사와 서버 관찰](testing/document-qa-indexing-profile-fix-20261012.json) |
| 검색 Tool 연결·버전 고정·시험/발행 gate | [Q06a 안내](document-qa/search-tool.md), [서버 로컬 인수](testing/document-qa-q06a-server-observed-20261008.json), [오프라인 검사](testing/document-qa-q06a-offline-20261008.json) |
| 질문 대상 저장·head/revision·호환 migration | [Q06b-1 안내](document-qa/question-storage.md), [서버 로컬 인수](testing/document-qa-q06b1-server-observed-20261008.json), [오프라인 검사](testing/document-qa-q06b1-offline-20261008.json) |
| 검증된 질문 head CAS·전환 receipt·API 정책 | [Q06b-3 안내](document-qa/question-head.md), [서버 로컬 인수](testing/document-qa-q06b3-server-observed-20261008.json), [오프라인 검사](testing/document-qa-q06b3-offline-20261008.json) |
| 고정 QA Agent·질문 접수/결과·인용 검증 | [Q06b-2 안내](document-qa/qa-agent.md), [오프라인 검사](testing/document-qa-q06b2-offline-20261008.json) · [서버 확인](testing/document-qa-q06b2-server-observed-20261008.json) |
| 문서 올리기·목록·준비 단계 화면 | [Q07a 안내](document-qa/screen.md), [서버 로컬 인수](testing/document-qa-q07a-server-observed-20261008.json), [개발 검사](testing/document-qa-q07a-offline-20261008.json) |
| 다른 PC의 문서 QA 접속 | [LAN 수신 주소 추가](document-qa/lan-access.md) |
| 질문·답변·출처 확인 | [Q08 안내](document-qa/question-screen.md), [서버 로컬 인수](testing/document-qa-q08-server-observed-20261008.json), [개발 검사](testing/document-qa-q08-offline-20261008.json) |
| head 활성화 후 화면이 확인 필요인 경우 | [2026-10-12 질문 UUID 호환 수정](document-qa/question-uuid-fix.md), [현재 관찰·회귀 검사](testing/document-qa-question-uuid-fix-20261012.json) |
| 문서 오류·CPU 단계 명시 재개 | [Q07b 안내](document-qa/error-recovery.md), [서버 로컬 인수](testing/document-qa-q07b-server-observed-20261008.json), [개발 검사](testing/document-qa-q07b-offline-20261008.json) |
| 세션 재개 | [인수인계](HANDOFF_NEXT_SESSION.md) |
| 공통 기준 | [설계 원칙](design-principles.md), [구조](architecture.md), [개발](development.md) |
| 검증 | [테스트 안내](testing/README.md), [이번 검증](testing/source-review-20261001.json) |
| 과거 작업 | [이력](history/README.md), [문서 최신화 기록](history/documentation-refresh-20261001.md), [파일 정리 정책](operations/project-cleanup.md) |

## 플랫폼과 운영

- [Data 문서](data-platform/README.md): 원본·수집·업로드·정제·그룹·OCR·품질.
- [Knowledge 문서](knowledge-platform/README.md): 청킹·색인/버전·검색/rerank·평가.
- [Agent 문서](agent-platform/README.md): Tool/MCP·OpenAPI·고정 QA·n8n·사용자 인수.
- [Ops 문서](ops-platform/README.md): 관측·조회·추적·경보·자동갱신·현재 백업 단계.
- [로컬 운영](operations/local.md), [빌드](operations/build.md), [GC](operations/gc.md), [저장소](operations/storage.md), [백업 범위와 복원](operations/backup-restore.md), [전달/적용 팁](operations/tips.md).

## Model 계약

| 기능 | 문서 |
|---|---|
| 모델 생성·관리·목록 | [요청 생성](reference/request-creation.md), [관리 수정](reference/model-management-update.md), [목록](reference/catalog.md) |
| 데이터셋·학습 | [데이터셋](reference/datasets.md), [학습](reference/training.md), [제어](reference/training-control.md) |
| 결과·재개·평가 | [출력 복구](reference/training-output.md), [checkpoint](reference/training-checkpoints.md), [평가](reference/training-evaluation.md) |
| 추론 | [서빙](reference/serving.md), [adapter](reference/adapter-serving.md), [embedding](reference/embedding.md), [reranker](reference/reranking.md) |
| 확장 | [DDP](reference/multi-gpu.md), [원격 노드](reference/remote-nodes.md), [LiteLLM 검토](reference/litellm-integration-review.md), [인프라 검토](reference/infrastructure-expansion-review.md) |

## 문서 갱신 규칙

1. 완료 상태는 `status.md`, 다음 순서는 `roadmap.md`에 우선 반영한다. 같은 긴 진행 문단을 README·HANDOFF·설계 원칙에 복사하지 않는다.
2. 구현은 코드 경로, 검증은 결과 파일/날짜/범위와 함께 기록한다. 로컬 대역·실제 서버·사용자 인수·품질 평가는 구분한다.
3. 실패를 후속 성공으로 삭제하지 않는다. 대신 해결 여부와 성공한 범위를 현재 문서에 명시한다.
4. API/운영 절차는 소유 문서 한 곳에 유지하고 다른 문서는 링크한다. 날짜별 문서의 실행 명령은 당시 절차이며 재실행 지시가 아니다.
5. 환경별 IP·현재 컨테이너 ID를 일반 사용자 기본값으로 문서화하지 않는다. 기존 설정/발견 결과를 기준으로 한다.
6. 한 단계의 코드/검증/상태/다음 작업을 함께 갱신한다. 문서만 변경할 때 기존 서비스 재시작을 요구하지 않는다. 새 연결 코드의 실제 실행/배포는 해당 단계에서 구분한다.
