# Agent Platform 아키텍처와 소유권

Agent에도 [자동화와 적응성](../design-principles.md)을 적용한다. 서비스와 제공 API/기능을 발견해 Tool·Connection 연결과 초기 설정을 줄이는 방향으로 확장한다. Tool/Agent의 고유 소유권, 확정한 버전·연결 신원·실행 정책은 유지한다. 발견한 API를 등록·발행·실행하는 수명주기는 해당 계약에 따르며 아래 현재 구현과 후속 자동화를 구분한다.

갱신일: 2026-09-23. A01 설계에 따라 [A02 기본 구조](foundation.md)를 구현했다. A02 서버 인수를 마쳤다. [A03 공통 Tool·MCP](tools-foundation.md) 구현·로컬 검증과 서버 인수를 2026-09-23에 마쳤다. OpenAPI/검색 Tool·Agent 실행은 [작업계획](work-plan.md)의 A04~A09에서 이어간다.

## 1. 목적과 독립성 기준

Agent Platform은 Tool과 Agent를 독립 자산으로 등록·버전 관리하고 실행하는 플랫폼이다. 다른 플랫폼의 기능은 해당 플랫폼이 소유한 API를 호출해 조합한다. 외부 플랫폼의 내부 저장소·실행기·상태 머신을 가져와 같은 기능을 중복 구현하지 않는다.

독립성은 별도 디렉터리·컨테이너 외에도 다음을 포함한다.

1. 리소스의 정의·버전·수명주기를 결정하는 소유자가 하나다.
2. 플랫폼 간 연결은 버전이 있는 공개 API 계약으로 한다. 다른 플랫폼의 DB·버킷·Docker·GPU·내부 runtime 주소를 직접 사용하지 않는다.
3. 다른 플랫폼의 작업 ID를 참조해 조회할 수 있지만 그 플랫폼의 상태를 직접 변경하거나 대신 성공 처리하지 않는다.
4. 호출 대상의 실패는 필요한 실행에만 전파한다. Model/Data/Knowledge의 기동과 독립 기능은 Agent·n8n의 존재를 요구하지 않는다.
5. n8n이 중지돼도 Agent API/MCP를 직접 사용할 수 있다. LangGraph 구현 세부와 n8n 식별자는 Tool/Agent의 고유 ID를 대체하지 않는다.

## 2. 플랫폼별 소유권

| 소유자 | 고유 리소스·정책 | 허용하는 API 이용 | 두지 않을 구현 |
|---|---|---|---|
| Model | 모델·모델 버전·학습·배포·추론·GPU/runtime 수명주기 | 외부 실행 요청 수신 | Tool 등록부·Agent 정의·Tool 실제 실행 |
| Data | 원본·수집·정제·파싱/OCR 처리 조립·문서 구조 판별·canonical | Model OCR 추론 요청 | 지식 인덱스·검색 정책·Agent 상태 |
| Knowledge | ChunkSet·색인 버전·활성화·검색·선택적 재정렬 정책·출처 | Data canonical 조회·참조 관리, Model 문서 임베딩·재정렬 점수 요청 | PDF 재해석·모델 직접 로딩·MCP/Agent 등록 |
| Agent | Tool·ToolVersion·Agent·AgentVersion·AgentRun·ToolCall·프롬프트·실행 조립 | Model 추론, Knowledge 검색, 등록된 외부 API | 원본 파서·별도 검색 인덱스·모델 배포 실행기 |
| n8n | n8n 워크플로의 노드 연결·상위 실행 기록·자체 로그인 | Agent/Tool 실행 API 호출, 외부 업무 연결 | 플랫폼 Tool/Agent 기준 정보의 중복 정의 |
| LangGraph | Agent Platform 내부의 그래프 실행·해당 실행 체크포인트 | Agent가 승인한 모델/Tool 어댑터 | 독립적인 제품 등록부·Model/Data/Knowledge 데이터 관리 |

Model이 향후 Tool schema를 추론 입력으로 받고 호출 제안을 반환하는 기능을 지원해도, 모델 출력의 검증·실제 Tool 호출·실행 기록은 Agent 책임이다. 현재 Model 채팅 API는 이 기능을 제공하지 않으며 첫 Agent는 정해진 검색·답변 흐름을 실행한다.

## 3. 같은 이름으로 혼동하지 않을 흐름

| 개념 | 기준 정보·실행 의미 |
|---|---|
| Tool | Agent가 정의한 입력·출력·버전·호출 계약. 단일 HTTP API 또는 여러 API를 조합한 Knowledge 검색 구현 |
| Agent 내부 흐름 | AgentVersion에 고정된 템플릿·모델·Tool·프롬프트·처리 정책. LangGraph가 실행 |
| n8n 상위 Flow | 입력 수신·Agent/Tool 호출·조건 분기·외부 결과 전달을 사용자가 연결. n8n에서 편집·저장 |
| WorkflowLink | Agent가 보관하는 n8n 인스턴스/워크플로 참조와 표시 이름. n8n Flow 정의의 두 번째 원본이 아님 |

n8n은 Agent를 하나의 호출 단계로 사용한다. n8n JSON을 임의의 LangGraph 그래프로 자동 변환하지 않는다. n8n 기본 AI Agent 노드에 플랫폼 Agent의 프롬프트·Tool 정책을 별도 복제해 제품 Agent로 관리하지 않는다.

n8n의 일반 자동화 노드 자체를 모두 Agent Tool로 재정의하지 않는다. 우리 플랫폼에 등록한 Tool/Agent를 사용하는 노드는 해당 ID·버전으로 Agent API를 호출한다. n8n의 전체 워크플로는 n8n 의존 자산이며 엔진 독립 포맷으로 내보내기를 보장하지 않는다. Tool·Agent는 n8n과 무관하게 유지한다.

## 4. 제안 서비스 구조

A02에서 gateway/service/runtime/contracts와 전용 postgres 구성을 생성했고 A03에서 mcp 서비스와 Tool 호출 실행을 추가했다. n8n은 A07에서 추가한다. 현재 구현·API·이미지/포트는 [A03 안내](tools-foundation.md)가 기준이다.

| 구성 | 책임 |
|---|---|
| agent_platform/gateway | 외부 REST·관리 UI 정적 자산·서비스 연결. 기존 Go Gateway 패턴 사용 |
| agent_platform/service | Go 관리 서비스. Tool/Agent 등록·버전·실행 접수·상태의 기준 저장소 |
| agent_platform/mcp | 고정 SDK의 MCP HTTP 서버·Tool 호출 어댑터. 관리/실행 서비스 계약 이용 |
| agent_platform/runtime | Python LangGraph 실행기·HTTP/검색 Tool 어댑터. 승인된 실행을 처리하고 결과 보고 |
| agent_platform/contracts | Agent 내부 gRPC 계약. Model/Data/Knowledge에 Agent 계약 의존성 추가 금지 |
| infra/n8n | n8n Compose·전용 PostgreSQL·비밀·볼륨·기동·진단. infra 상위 include |
| agent_platform/n8n | Agent 연동 Flow 템플릿·생성기·WorkflowLink 등록·격리 연동 검사 |
| agent_platform/postgres | A02 전용 PostgreSQL DB/계정 초기화·별도 볼륨. 관리 서비스가 Agent migration 적용 |
| agent_platform/docker-compose.yml | 서비스별 Compose include. 전용 private network, Gateway/runtime만 기존 ops-infra 연결 |

관리와 실행 제어는 내부 gRPC, 외부 UI/n8n은 REST, MCP 클라이언트는 MCP를 사용한다. 기존 플랫폼은 공식 Gateway REST를 소비하며 DB나 runtime 포트로 우회하지 않는다. A02 내부 gRPC는 전용 Docker network의 TCP로 고정했다. 공개 기본 포트는 16100이고 이미지 digest/SDK pin을 추가했다. 실제 서버 충돌 여부는 preflight로 확인한다. MCP는 A03에서 공식 Go SDK 1.8.0·프로토콜 2025-11-25·stateless Streamable HTTP로 고정했다. LAN 접속이 필요한 기존 사용 방식을 유지한다.

공통 Tool마다 컨테이너를 생성하지 않는다. MCP와 실행기는 상시 서비스로 두며 HTTP/검색 호출을 처리한다. Agent 실행에 기존 Model의 Operation/Dispatcher를 가져다 쓰지 않는다. 기존 lease·멱등 설계 개념은 참고할 수 있으나 Agent 상태와 실행 승인은 Agent가 소유한다.

## 5. API 호출 경로

| 요청 | 호출 경로 |
|---|---|
| OpenAPI Tool | 호출자 → Agent MCP/REST → Tool 실행기 → 등록된 대상 API |
| 자연어 검색 Tool | 호출자 → Agent Tool → Knowledge 버전 조회 → Model 질문 임베딩 → Knowledge 검색 |
| 재정렬 ON 검색 | 위 경로의 Knowledge 검색 → Model 재정렬 점수 → Knowledge 최종 순위 |
| 첫 질의응답 Agent | 우리 UI/n8n → AgentRun → LangGraph → 등록 검색 Tool → Model 채팅 → 출처 포함 답변 |
| 향후 수집·배포 Tool | Agent Tool → Data/Model 작업 API → 해당 소유자의 작업 조회. 장기 작업 자동 대기는 후속 |

Knowledge 검색 Tool은 자연어 인터페이스·버전 고정·API 조립을 소유한다. 청킹·후보 생성·재정렬 순위 계산은 Knowledge에 유지한다. Data 원본을 다시 읽어 청킹하거나 OpenSearch에 직접 접근하지 않는다.

## 6. 저장과 실행 상태

- Agent 전용 DB 또는 전용 schema/계정을 사용한다. Tool/Agent/실행 기록은 Agent 관리 서비스만 변경한다.
- A06은 LangGraph `compile(checkpointer=False)`를 사용한다. 내구성 있는 AgentRun/5단계 기록은 Service DB가 소유하고 Runtime은 내부 Service callback으로만 기록한다. Runtime에 DB 자격을 부여하지 않는다. 별도 checkpoint 기반 재개를 도입한다면 Agent 소유 namespace와 외부 호출 재개 정책을 후속 설계한다.
- n8n DB는 워크플로·자체 실행·n8n 인증정보의 원본이다. Agent가 n8n DB를 직접 읽거나 수정하지 않는다.
- 같은 PostgreSQL/MinIO 인프라를 사용해도 Model/Data/Knowledge의 테이블·객체는 Agent가 직접 사용하지 않는다. 결과 원문/바이너리 접근이 필요하면 소유 플랫폼의 API를 이용한다.
- 요청 수락과 실행 결과는 [Agent 계약](api-agents.md)의 상태 전이에 따른다. 타 플랫폼 operation/job ID, n8n execution ID, LangGraph thread/checkpoint ID는 참조로 연결한다.
- 런타임 중단 후 API 재호출이 외부 효과를 중복시킬 수 있다. 체크포인트 저장이 외부 API의 exactly-once 실행을 보장한다고 가정하지 않는다.

## 7. UI와 접근 범위

A06 UI는 Tool 등록·OpenAPI 가져오기·Agent 설정·질문·실행 결과를 제공한다. A07은 WorkflowLink 참조 API를 제공하며 A08의 Flow 편집 버튼은 설정된 n8n 주소를 새 탭으로 열 예정이다. n8n은 별도 로그인한다. 사용자 계정·권한의 완전 통합이나 다중 고객 격리는 MVP 완료 범위가 아니다.

MVP는 기존 신뢰된 개발/사내망을 기준으로 서비스 토큰과 비밀 참조를 구분한다. n8n에 대상 외부 API 비밀을 복제하지 않고 Agent 호출용 자격만 설정한다. 고객용 공개 운영에는 각 플랫폼의 소유권/권한 계약을 확장해야 한다.

iframe·SSO·n8n 내부 화면 변경은 후속이다. 새 탭 사용 여부 자체가 고객 제공 라이선스를 결정하지 않는다. 개인/사내 개발과 고객용 서비스의 제공 형태를 구분한다. 공식 OEM 문서는 제품 안에 편집기를 제공할 때 별도 상용 계약과 n8n 브랜딩을 안내한다. Token Exchange 문서는 Enterprise/Preview로 안내하므로 후속 채택 시 버전·계약을 다시 확인한다. 이번에는 해당 기능을 설치하거나 라이선스 계약을 체결하지 않는다.

## 8. 변경 경계와 검증

Agent 추가를 위해 Model/Data/Knowledge의 도메인 코드·DB·원본·인덱스를 변경하는 것을 기본 전제로 삼지 않는다. 기존 API에 실제 누락이 발견되면 해당 플랫폼 소유의 별도 변경으로 제안하고 계약/검증 범위를 분리한다.

독립성 인수는 n8n 없는 Agent 직접 실행, Agent 없는 기존 플랫폼 기능, 다른 플랫폼 저장소 자격 없이 API 연동, 상대 플랫폼 장애 시 오류 전달로 확인한다. 상세 검증은 [작업계획](work-plan.md)을 따른다.

## 9. 근거

- 프로젝트: [전체 경계](../architecture.md), [Knowledge 검색](../knowledge-platform/api-search.md), [Model 임베딩](../reference/embedding.md), [Model 채팅 코드](../../model_platform/gateway/app/chat.go).
- LangGraph: [실행 프레임워크](https://docs.langchain.com/oss/python/langgraph/overview), [상태 보존](https://docs.langchain.com/oss/python/langgraph/persistence).
- n8n: [OEM](https://docs.n8n.io/deploy/host-n8n/deploy-as-an-oem-integration), [Token Exchange 원문](https://github.com/n8n-io/n8n-docs/blob/main/docs/deploy/host-n8n/deploy-as-an-oem-integration/set-up-token-exchange.md), [라이선스](https://github.com/n8n-io/n8n/blob/master/LICENSE.md).

공식 자료는 2026-09-22 논의에서 확인했다. 제품 선택은 본 프로젝트의 설계 결정이며 특정 버전의 통합 성공·최신 가격 확인을 뜻하지 않는다.

A04 구현에서도 명세·Tool 버전·실행 기록은 Agent DB에 두고 외부 기능은 HTTP 공개 API로만 호출한다. [현재 OpenAPI/HTTP 경계](openapi-foundation.md)를 따른다.

## 10. 자연어 검색 조합·서비스 관리 후속 결정

2026-09-23 논의는 [개발 방향](tool-composition-and-service-catalog-20260923.md)에 기록했다. A05에서 Agent Tool이 Model 임베딩과 Knowledge 검색을 정해진 순서로 연결한다. 서비스별 API 목록·연결 관리는 실행 조합과 구분하며 A05 이후 작은 범위로 추가한다. 직접/OpenAPI는 등록 방법으로 유지한다. 일반 앱까지 공통 자연어 검색을 재사용할 요구가 생기면 Knowledge의 상위 API를 검토하고 Agent는 그 API를 호출하도록 정리한다. 이번 기록은 A05·카탈로그·범용 조합 구현 완료를 뜻하지 않는다.

A05 진행: [자연어 검색 어댑터 구현·인수 범위](knowledge-search-a05-20260923.md). 고정 API 조합을 기존 Tool 실행기에서 처리하며 외부 플랫폼 실행 코드는 변경하지 않는다.

## n8n 운영 배치 결정 — 2026-09-24

사용자 결정에 따라 n8n의 운영 수명주기는 infra가 소유한다. `infra/ops/up.py`로 초기 infra 실행에 포함하며 Agent에는 활용 코드만 둔다. n8n과 전용 DB는 `infrastructure` Compose 프로젝트로 관리하고 기존 A07 키·외부 볼륨을 보존한다. Agent의 기동/health에 n8n을 필수 의존성으로 추가하지 않는다. [이전·검증·현재 서버 제한](n8n-infra-20260924.md)을 따른다.
