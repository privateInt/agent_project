# Agent Platform 문서

기존 Agent에는 Tool/MCP·Knowledge 검색·고정 QA·n8n·관리 UI가 구현돼 있다. 문서 QA 연결 계층의 진행 근거는 [현재 상태](../status.md), 다음 작은 작업은 [계획서](../plans/document-qa-work-plan-20261002.md)에 관리한다. TXT 실제 CPU 연결과 Q05b 보류 callback은 확인했으나 실제 QA 색인/질문·화면·품질은 후속이다. 일반 Flow 편집은 별도 확장이다.

## 구현과 사용

| 기능 | 문서 |
|---|---|
| 소유권·기본 서비스/설정 | [구조](architecture.md), [기반](foundation.md) |
| Tool·Connection·MCP | [Tool API](api-tools.md), [공통 Tool](tools-foundation.md) |
| OpenAPI·직접 등록 | [OpenAPI](openapi-foundation.md), [등록 흐름](tool-registration-20260923.md) |
| Knowledge 자연어 검색 | [검색 Tool](knowledge-search-a05-20260923.md) |
| 고정 QA Agent | [Agent API](api-agents.md), [QA 실행](knowledge-qa-a06-20260924.md) |
| n8n | [연동](n8n-a07-20260924.md), [운영](n8n-operational-20260924.md), [세션 복구](../history/agent-platform-202609.md#n8n-session-recovery-20260925) |
| 관리 UI | [A08 콘솔](console-a08-20260924.md) |
| 운영/인수 | [A09 절차](operations-a09-20260924.md), [인수표](acceptance-a09-20260924.md) |

## 검증 근거와 후속

- [A02](../history/agent-platform-202609.md#testing-foundation-20260922), [A03](../history/agent-platform-202609.md#testing-tools-20260922), [A06](../history/agent-platform-202609.md#a06-server-check-20260924), [A08](../history/agent-platform-202609.md#a08-server-check-20260924).
- [인용 수정](../history/agent-platform-202609.md#citation-verified-20260925), [운영 전달](../history/agent-platform-202609.md#n8n-operational-verified-20260925), [재시작 보존](../history/agent-platform-202609.md#restart-verified-20260925).
- [의미 품질 후속](answer-quality-followup-20260924.md), [schema 자동완성 후속](schema-autofill-followup-20260923.md), [서비스 API 관리와 조합](tool-composition-and-service-catalog-20260923.md).

서비스 API 카탈로그/범용 schema 자동완성, 임의 내부 그래프·자율 Tool 선택·SSO/iframe은 별도 확장이다. Knowledge 전용 자동 설정과 OpenAPI 지원 명세를 가져오는 현재 기능을 범용 자동화 완료로 확대하지 않는다. Ops는 이미 별도 플랫폼으로 구현됐으며 [Ops 현재 범위](../ops-platform/README.md)를 참고한다.
