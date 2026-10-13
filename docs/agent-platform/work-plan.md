# Agent Platform 단계별 상태와 후속

2026-10-01 코드와 보존된 서버 결과를 대조했다. 초기 계획의 A01~A10 순서를 유지하되 현재 구현/인수 범위로 갱신했다. 신규 개발 순서는 [전체 로드맵](../roadmap.md)에 둔다. 2026-10-02 사용자가 선택한 첫 완성 목표와 새 연결 작업은 [문서 QA 작업계획서](../plans/document-qa-work-plan-20261002.md)를 따른다.

| 단계 | 구현 범위 | 현재 인수 상태 |
|---|---|---|
| A01 | 플랫폼 소유권·Tool/Agent 계약 | 문서/구조 있음. 독립성 원칙 유지 |
| A02 | Gateway·Service·Runtime·전용 DB·상태/진단 | 과거 서버 smoke·DB·재시작 조회 통과 |
| A03 | Connection·Tool 버전·시험/발행·REST/MCP·실행 기록 | 과거 서버 인수·재시작 기록 보존 통과 |
| A04 | OpenAPI 3.0/3.1 지원 부분집합·직접 HTTP 등록·화면 | 구현/로컬 검사·사용자 시험 성공 근거. 전체 지원 조합/운영 인수는 별도 |
| A05 | 자연어 질문→Model embedding→Knowledge 검색/rerank | 구현·사용자 검색 시험 및 후속 Agent/n8n 경로 근거. 질문 관련성·전체 standalone MCP 인수는 구분 |
| A06 | `knowledge_qa_v1`·Agent 버전·5단계 LangGraph·결과/출처 | 실모델 실행·완료/실패 기록. 인용 형식과 의미 품질은 별도 |
| A07 | infra n8n·API 호출·운영 Flow·실행 ID/재사용 | 전달/동일 요청 재사용·재시작 보존 통과. 사용자 직접 Flow 편집 인수 보류 |
| A08 | Tool·Agent·실행 결과·n8n 새 탭 링크 UI | 구현/서버 갱신/읽기 확인 근거. 최종 사용자 화면 인수 보류 |
| A09 | 통합 인수·capture/compare·운영/복구 안내 | 7개 Agent/n8n 컨테이너 재시작과 고정 기록 보존 PASS. 의미 품질/화면 남아 MVP 전체는 미완료 |
| A10 | 독립 Ops 관측·API·화면·추적·경보 | O01~O04 확인 범위까지 진척. 자동갱신/boot·복구 구현, 운영 백업은 현재 미완료. [Ops](../ops-platform/README.md) |

## 고정한 현재 계약

Tool/Connection/Agent는 버전과 실행 입력을 고정하고 재전송은 기존 실행을 재사용한다. OpenAPI 전체 스펙을 지원한다고 가정하지 않으며 미지원 항목을 표시한다. runtime은 다른 플랫폼 DB·저장소를 직접 변경하지 않고 Model/Knowledge API로 호출한다.

내부 Agent 단계는 `validate → search → context → generate → finalize`다. n8n은 이 실행을 호출하는 상위 흐름이다. 임의 그래프·자율 Tool 선택·checkpoint replay를 현재 기능으로 표시하지 않는다.

## 현재 문서 QA 목표

사용자는 문서 업로드와 질문만 수행한다. 상위 문서 준비 workflow가 Data 정제·Knowledge 색인·Agent 연결을 진행하고 질문은 기존 고정 QA로 실행한다. 새 색인에 맞는 Tool/Agent 버전은 Agent가 소유하며 기존 버전을 덮어쓰지 않는다. 문서 QA 연결 계층은 해당 참조와 현재 질문 대상 revision만 관리한다.

[작업계획서](../plans/document-qa-work-plan-20261002.md)의 Q06에서 자동 Tool/Agent 연결, Q08에서 질문/출처 화면, Q10에서 근거/인용 품질, Q11에서 사용자 인수를 다룬다. 현재 Q01/Q02 확인, Q03a/Q03b/Q04a 서버 적용·자기 확장과 Q04b [자동 CPU 정제 Flow](../document-qa/refinement-workflow.md)·32개 검사까지 진행했다. 다음 실제 CPU 연결 인수와 GPU 연동 보류를 [현재 상태](../status.md)에 기록했다. 기존 Agent 실행 코드/DB는 변경하지 않았다. 일반 Flow 편집·범용 그래프·API 카탈로그는 별도 후속이다.

## 남은 인수와 작업

1. 문서 QA 사용자는 업로드·질문·결과/오류 확인만 하도록 자동 연결과 전용 화면을 인수한다. 기존 관리 UI의 Tool/Agent 등록과 일반 n8n 편집 인수는 후속으로 구분한다.
2. 문서 QA 계획의 Q10에서 대표 질문으로 반복·근거 충실도·인용 정확성·모름 응답·지연을 평가한다. 기존 개선 prompt의 전송 성공을 의미 품질 PASS로 처리하지 않는다. 아직 새 품질 결과는 없다.
3. 내부 서비스 API 카탈로그와 schema/연결 자동완성은 현재 Connection·OpenAPI와 Knowledge 전용 흐름을 바탕으로 별도 구현한다.
4. 임의 그래프·다중 Agent·다중 사용자 권한은 실제 요구가 생긴 뒤 추가한다.

[상세 인수표](acceptance-a09-20260924.md), [서버 결과 JSON](../../src/platform/agent/tests/results/a09-server-20260925.json), [Tool API](api-tools.md), [Agent API](api-agents.md), [구조](architecture.md), [서비스 관리 방향](tool-composition-and-service-catalog-20260923.md)를 근거로 사용한다.
