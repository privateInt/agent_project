# A05 자연어 지식 검색 Tool

후속 A06 문서 질의응답 Agent는 [별도 적용·사용 안내](knowledge-qa-a06-20260924.md)를 따른다. 아래 A05 검증/인수 범위는 당시 근거로 유지한다.

2026-09-23. 구현·로컬 검증 완료. 사용자 화면 발췌에서 시험 성공·검색 결과 5개 표시를 확인했으며, 관련성·게시·전체 서버 인수는 미확인이다. [서버 화면 근거와 다음 확인](../history/agent-platform-202609.md#a05-server-ui-check-20260923). 빌드 `a05.1`, RPC 계약 `agent.platform.v1/a05`, DB schema `3`이다. A03 기록과 기존 HTTP Tool을 유지한다. A04의 사용자 검색 Tool 시험 성공은 기존 근거이며, 미확인된 A04 게시·전체 서버 인수를 이번 로컬 검사로 대신 완료 처리하지 않는다.

## 사용자가 달라지는 점

1. `/console`에서 기존 Agent API 토큰으로 연결한다.
2. **도구 추가 → 지식 검색 → 지식 목록 불러오기**를 누른다.
3. **test1**을 고르고 **다음: 도구 설정 → 저장 후 시험하기**를 누른다.
4. 선택한 지식에 포함된 내용을 질문하고 **시험 실행**을 누른다. 예를 들어 가죽 생산 전과정평가 문서를 선택했다면 `이 연구에서 분석한 투입물과 산출물은 무엇인가요?`처럼 묻는다. 지식 이름만으로 문서 주제를 가정하지 않는다.
5. 본문·출처와 성공 상태를 확인한 뒤 **사용 시작**을 누른다. 이후 **도구 실행** 또는 REST/MCP로 사용한다.

처음에는 Knowledge와 Model 서비스 연결 두 개를 저장한다. 기본 주소는 runtime에서 접근하는 `http://knowledge-gateway:8000`, `http://model-gateway:8000`이다. 같은 `ops-infra` 네트워크의 기존 배포를 사용한다. 환경이 다르면 **서비스 연결 설정**에서 주소나 저장된 인증 연결을 선택한다. 진단용 `A04 test endpoint`, `explicit API`, `missing credential test`를 이름만 보고 선택하지 않는다. 이 주소들은 Agent 관리 화면의 `16100` 접속 주소와 다르다.

질문 벡터, index/version/space/deployment ID와 schema는 직접 입력하지 않는다. 검색할 지식을 조회해서 내부 값을 고정한다. 목록/설정 확인은 조회만 수행하고, 시험 실행부터 Model 질문 임베딩 1회와 Knowledge 검색 1회를 실행한다. 도구 생성 시 모델 배포·재색인·참조 보유 변경은 수행하지 않는다.

결과는 관련 문서 조각이다. 답변 문장 생성·LLM 요약은 다음 A06 범위다. 기존 **API 직접 등록**과 **OpenAPI 가져오기**는 별도 API 연결에 계속 사용할 수 있다. 모든 내부 서비스의 schema 자동완성이나 서비스별 API 카탈로그·범용 조합 편집기는 이번 범위가 아니다.

## 서버 적용

패치 기준은 사용자가 적용한 `agent_platform_tool_registration_20260923`이다. 알려진 중간 문서 갱신본도 허용한다. 설정·비밀·볼륨은 적용 대상이 아니다. 다른 로컬 변경이 있으면 적용 전에 중단한다. 문서만 바뀐 패치와 달리, 이번에는 Agent 서비스 이미지를 재빌드해야 한다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_a05_20260923.tar.gz
python3 agent_platform_a05_20260923/apply_patch.py \
  --project-root ~/workspace/agent_project --apply

cd ~/workspace/agent_project/agent_platform
python3 ops/preflight.py --skip-port-check
docker build --target tests -t agent-platform-go-tests:a05.1 .
docker build -f runtime/Dockerfile --target tests \
  -t agent-platform-python-tests:a05.1 .
docker compose up -d --build --wait
python3 ops/smoke_tools.py --call-id 8e3b3160-8e29-49d8-96d2-e0540e67682e
docker compose --profile checks run --build --rm agent-db-checks
)
```

적용 뒤 브라우저를 새로고침하고 토큰으로 다시 연결한다. 기존 A03 호출 조회의 request ID는 `b611f3d7-8d9f-4dc1-9110-04496f5278ea`여야 한다. 기존 A03 Tool은 비활성화/폐기 상태일 수 있으므로 새 실행을 요구하지 않는다. DB 검사에 A05가 추가되지만 외부 Knowledge/Model은 격리 HTTP 대역이다. 실제 기존 지식·GPU 인수는 위 화면 순서로 수행한다.

화면에서 **사용 시작**까지 마친 뒤, 선택적으로 실제 REST/MCP 공통 호출을 확인할 수 있다. 아래 명령은 해당 발행 도구를 한 번 더 실제 실행한다. 같은 state 파일로 재실행하면 동일 호출을 조회/재전송한다.

```bash
python3 ops/smoke_knowledge.py \
  --tool-name 'test1 검색' \
  --question 'AI 엔지니어에게 필요한 역량은 무엇인가요?' \
  --state-file ~/workspace/agent_a05_search_check.json
```

도구 이름을 바꿨다면 `--tool-name`도 맞춘다. 다른 질문으로 새 실행하려면 새 state 파일을 사용한다. 토큰·질문 본문·검색 결과는 state 파일에 넣지 않으며 버전·멱등 키·입력 해시만 보관한다. 정상 출력에서 `result: passed`, `mode: rest_and_mcp`와 실행·요청 ID를 보관한다. 재시작 후에는 `--call-id <보관한 ID> --request-id <원래 request ID>`로 조회만 수행한다.

## 구현 계약

- `POST /api/knowledge/inspect`는 관리 화면의 읽기 전용 discovery다. 기존 관리 인증과 UUID `Idempotency-Key` 헤더 형식을 적용하되 결과를 저장하거나 키를 멱등성 테이블에 등록하지 않는다. 매 요청이 현재 상태를 조회한다. `action=indexes`는 100개씩 cursor 페이지를 반환하고, `action=resolve`는 선택한 인덱스 버전과 모델 호환성을 확인한다.
- Service가 DB의 두 ConnectionVersion을 읽어 Runtime에 전달한다. 외부 대기 중 DB 트랜잭션을 유지하지 않는다. 임의 URL/HTTP operation을 discovery 입력으로 받지 않는다.
- `knowledge_search_v1` ToolVersion의 `binding.connection_version_id`는 Knowledge 연결이고, `binding.knowledge`는 Model 연결·인덱스/버전·공간·배포 ID·기본 검색 개수를 보관한다. 두 연결은 ToolCall snapshot에 저장한다.
- 실행 시 인덱스 `active`, 선택 버전 `ready`/`held`/색인 완료, Model의 동일 배포·동일 공간·1024차원·L2·ready·reject 정책을 검사한다. 현재 active 버전이나 Connection current 포인터를 따라가지 않는다. 삭제된 버전·중지된 모델이면 실패한다. Agent는 외부 버전의 삭제를 막는 참조를 생성하지 않으므로 운영자는 사용하는 외부 버전을 유지해야 한다.
- 입력 `query`는 공백을 자르거나 접두어를 붙이지 않고 `role=query`로 Model에 전달한다. 최대 8 KiB, 빈 질문/NUL 거절. `top_k` 1~50, `candidate_k` top_k~100. 후보 수를 생략하면 설정 기본값과 top_k 중 큰 값을 사용한다. API/JSON 입력에서 source_document_ids/canonical_ids/kinds 필터를 사용할 수 있다.
- embedding receipt의 전체 profile·role·입력 ID·순서·토큰·1024개 유한수·L2 norm 오차 0.001 이하를 검사한다. 벡터를 자동 정규화하거나 잘라서 통과시키지 않는다.
- 기본 rerank OFF는 `query_text`를 보내지 않는다. API에서 `rerank_deployment_id`를 resolve에 지정하면 profile ID를 고정한 binding을 반환한다. ON은 원문과 고정 reranker ID/profile을 Knowledge에 전달한다. UI는 OFF로 시작하며 reranker 선택 UI는 후속이다. 실패 시 OFF 검색으로 fallback하지 않는다.
- 반환 본문·원본 spans·quality·순위/점수·추가 필드는 유지한다. 별도 metadata에 실제 embedding profile·연결/지식 버전·단계 시간을 기록한다. 질문 벡터는 결과에 복제하지 않는다.
- 전체 기본 180초, rerank 포함 기본 240초, 최대 240초. 각 조회 5초, embedding 120초, 검색 OFF 15초/ON 60초 및 부모 deadline 중 작은 시간을 적용한다. 외부 호출은 재시도하지 않는다.
- HTTP 전송의 DNS/IP 고정·주소 차단·인증 참조·비밀 제거·응답 크기 제한을 재사용한다. discovery는 총 18초, 응답은 2 MiB 제한이다.
- 초안의 성공 시험 뒤에만 게시한다. REST/MCP는 동일 ToolCall 접수·멱등 키·lease/fencing·기록을 사용한다. MCP 응답 대기(35초)가 실행보다 먼저 끝날 수 있으며 이때 반환된 ID 또는 같은 키로 기존 실행을 확인한다.

## 검증 근거와 남은 인수

[작은 검증 요약](../../agent_platform/tests/results/a05-local-20260923.json)에 범위를 기록한다. 로컬에서는 실제 Go↔Python gRPC, HTTP 대역, PGlite(PostgreSQL 18.3 WASM), REST/MCP SDK와 브라우저를 사용했다. 이후 사용자 서버 UI 시험 성공 보고는 [별도 기록](../history/agent-platform-202609.md#a05-server-ui-check-20260923)과 [작은 서버 요약](../../agent_platform/tests/results/a05-server-ui-20260923.json)에 추가했다. Docker 이미지 빌드·PostgreSQL 16·실행 metadata·게시/REST/MCP·재시작 보존의 전체 확인은 남아 있다.

- 정상 자연어/빈 결과, 버전/연결 고정, profile·receipt·벡터 오류, 필터 범위, rerank 명시·실패, timeout·무재시도.
- REST/MCP 동일 결과와 멱등 재조회, 게시 시험 조건, 실패 단계 저장, 기존 진단/OpenAPI 회귀.
- 브라우저 지식 목록→선택→저장→자연어 시험→본문/출처→게시→실행→기록 조회, 모바일 폭, 토큰 비저장.
- 실제 브라우저에서 textarea의 읽기 전용 type 속성 설정 오류를 발견해 수정했다. 검증용 Chromium 폰트 설정과 테스트 assertion 오류도 수정 후 다시 통과했다. 최초 실패를 성공 기록으로 숨기지 않는다.
