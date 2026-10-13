# Agent Platform

Agent가 Connection·Tool·OpenAPI 명세·발행 버전·ToolCall·AgentRun을 소유한다. 외부에는 REST/MCP, 내부에는 Go Service와 Python runtime을 사용한다. 다른 플랫폼은 공식 API로 호출한다.

현재 구현은 OpenAPI/직접 HTTP Tool 등록, Knowledge 자연어 검색, `knowledge_qa_v1` 고정 LangGraph 질의응답, n8n 연동과 관리 UI다. n8n은 infra의 상위 흐름 편집기이며 내부 Agent 그래프를 자유롭게 편집하는 기능과는 다르다.

기반 Tool/MCP와 실모델 실행, 운영 n8n 답변 전달/재사용 및 재시작 보존에는 서버 근거가 있다. **사용자 화면/Flow 편집 인수와 답변 의미 품질은 남아 있어 전체 MVP 인수는 미완료다.** 인용 형식을 지켜도 질문 반복 등 품질 문제는 별도로 평가한다.

- [문서 목차와 구현 범위](../docs/agent-platform/README.md)
- [단계별 현재 상태](../docs/agent-platform/work-plan.md)
- [소유권/구조](../docs/agent-platform/architecture.md), [Tool API](../docs/agent-platform/api-tools.md), [Agent API](../docs/agent-platform/api-agents.md)
- [설정/기본 기동](../docs/agent-platform/foundation.md), [Tool/MCP](../docs/agent-platform/tools-foundation.md), [OpenAPI](../docs/agent-platform/openapi-foundation.md)
- [n8n 운영](../docs/agent-platform/n8n-operational-20260924.md), [재시작 근거](../docs/history/agent-platform-202609.md#restart-verified-20260925)

Go module/protobuf는 빌드 컨테이너 안에서 생성한다. 설정/키와 기존 실행 ID를 보존하고 문서 갱신만으로 재기동·새 추론을 수행하지 않는다.
