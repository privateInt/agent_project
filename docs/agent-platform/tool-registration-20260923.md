# API 직접 등록·OpenAPI 가져오기와 지식 검색 시험

2026-09-23. 사용자는 앞선 간소화 화면이 서버에 표시된 것을 확인했다. 이후 요청 주소와 schema를 직접 넣는 등록 방식과 OpenAPI 문서 등록을 나누고, 기존 Knowledge 검색 API로 시험하기를 요청했다. 두 등록 경로와 시험 준비 도구를 구현했다. **로컬 검증과 사용자 서버의 패치 22개 파일 적용·Gateway 빌드/Healthy를 확인했다. 질문 임베딩 1회와 검색 API 직접 호출은 5개 결과로 통과했고, 이후 Agent Tool 시험 성공 화면을 확인했으며 게시·발행 후 실행 확인은 대기**다.

최신 사용자 확인: `Cap 2026-09-23 11-38-57-376.png`에서 `mode: test`, `state: succeeded`와 검색 응답을 확인했다. 아래 절차는 성공까지 진행한 사용법으로 보존한다. 다음은 `사용 시작`으로 게시한 뒤 발행된 도구 실행 확인이다. [내부 schema 자동완성과 사용자 입력 간소화](schema-autofill-followup-20260923.md)는 후속 요구로 기록했다.

## 현재 화면에서 이어서 시험하기

첨부 화면은 접속 토큰이 비어 있는 연결 전 상태다. `이 입력란을 작성하세요`는 브라우저 필수 입력 안내이며 이 화면만으로 서버 인증 실패를 뜻하지 않는다. 이때 목록 0개는 실제 등록 도구가 없다는 근거가 아니며 도구 추가·실행은 연결 후 사용할 수 있다.

### 1. Agent 화면 연결

사용자 서버 터미널에서 다음 명령으로 토큰을 확인한다.

```bash
cat ~/workspace/agent_project/agent_platform/.secrets/api-token
```

출력된 한 줄을 화면의 `접속 토큰`에 붙여 넣고 `연결하기`를 누른다. `Bearer ` 접두어는 넣지 않는다. 토큰 값은 대화나 검증 기록에 보내지 않는다. 페이지를 닫거나 새로고침하면 다시 입력한다. 토큰 파일이 없으면 configure를 다시 실행해 비밀을 재생성하지 말고 실제 Compose의 agent-api-token 파일 위치를 확인한다.

이 토큰은 Agent 관리 화면 접속용이다. 아래의 `인증 없이 연결`은 대상 Knowledge API의 접속 방식으로 서로 다른 설정이다.

### 2. 이미 준비된 파일 사용

준비 명령의 서버 로그에서 다음을 확인했다. 이후 Agent 화면 시험은 별도로 성공했다.

| 확인 | 결과 |
|---|---|
| 직접/OpenAPI 패치 | 22개 파일 적용 |
| Gateway | 이미지 빌드 완료·컨테이너 Healthy |
| 검색 시험 입력 | `/home/shshin/workspace/knowledge_tool_test_20260923`에 준비 완료 |
| 질문 임베딩 | 1회, lifecycle_writes 0 |
| 검색 API 직접 호출 | passed, returned_count 5, rerank false |
| 준비 명령 당시 Agent Tool 실행 | not_run; 이후 화면에서 test 성공 확인 |

**준비 명령은 다시 실행하지 않는다.** VS Code Remote SSH의 탐색기에서 위 폴더의 `tool-arguments.json`을 찾아 우클릭 → 다운로드로 브라우저를 사용하는 PC에 저장한다. 원격 서버의 파일은 로컬 브라우저 파일 선택 창에서 직접 보이지 않는다. 파일을 내려받기 어렵다면 서버에서 아래 내용을 복사해 시험 화면의 JSON 입력칸에 붙여 넣을 수 있다.

```bash
cat ~/workspace/knowledge_tool_test_20260923/tool-arguments.json
```

### 3. 검색 API 등록

1. `+ 도구 추가` → `API 직접 등록`을 선택한다.
2. 아래의 `지식플랫폼 검색 API로 시험하기`를 펼치고 `검색 API 예시 채우기`를 누른다. 요청 주소·입력/응답·Path schema가 채워진다.
3. `다음: 연결 설정`을 누르고 도구 이름은 `지식 검색 시험`으로 입력한다.
4. `+ 새 서비스 연결하기`에서 서비스 이름 `지식플랫폼`, 서비스 주소 `http://knowledge-gateway:8000`, 접속 방식 `인증 없이 연결`을 확인하고 `서비스 연결 저장`을 누른다. 이미 같은 주소와 인증의 연결이 있으면 선택해서 사용한다.
5. `고급 설정`의 `외부 데이터 변경 여부`를 `조회만 수행함을 확인함`으로 선택한다. 이 검색 API는 조회 기능이다.
6. `저장 후 시험하기`를 누른다.

기본 Compose의 같은 ops-infra 네트워크를 전제로 한 주소다. 화면의 Agent 토큰을 Knowledge의 인증 키로 넣지 않는다. 새 Tool은 아직 벡터 입력을 받는 일반 HTTP Tool이다. 자연어만 받는 검색 Tool은 [A05 개발 방향](tool-composition-and-service-catalog-20260923.md)대로 후속 구현한다.

### 4. 시험 실행과 판정

1. `시험 입력 파일 불러오기`를 펼친다.
2. PC로 내려받은 `tool-arguments.json`을 선택하고 `입력 불러오기`를 누른다. 또는 `JSON으로 직접 입력`을 선택하고 파일 내용 전체를 붙여 넣는다.
3. `시험 실행`을 누른다. 파일을 읽기만 한 상태에서는 API를 호출하지 않는다.
4. 성공 여부와 응답의 `results`·본문·출처를 확인한다. 직접 검색의 기준 결과는 5개였지만 실제 Tool 시험 결과를 별도로 확인해야 한다. ToolCall ID와 request ID를 기록한다.
5. 시험 성공 후 `사용 시작`을 누르면 발행한다. 이후 `도구 실행`으로 발행된 버전의 실행을 확인할 수 있다.

실패했다면 화면의 오류 코드·단계와 ToolCall ID를 확인한다. 질문 벡터나 토큰 전체를 복사해 보낼 필요는 없다. 최신 화면에서는 시험 성공을 확인했다. 게시·발행 후 실행은 별도 확인한다.

OpenAPI 경로로 시험하려면 같은 준비 폴더의 `openapi.json`도 PC로 가져와 `OpenAPI 가져오기` → 파일 선택 → `파일 분석` → 검색 기능 선택으로 시작한다. 이후 연결·시험 입력·실행 순서는 동일하다. 두 방식을 비교하는 시험이 아니라면 하나만 선택해도 된다.

## 두 등록 방식

| 등록 방식 | 입력 | 다음 단계 |
|---|---|---|
| API 직접 등록 (기본) | 전체 URL·GET/POST 등 요청 방식·입력 전달 위치와 JSON Schema·성공 코드·응답 schema | 연결·인증 선택 → 초안 저장 → 시험 → 사용 시작 |
| OpenAPI 가져오기 | OpenAPI 3.0/3.1 JSON/YAML 파일 → 지원되는 기능 선택 | 같은 연결·저장·시험·사용 흐름 |

직접 등록은 문서 파일을 별도로 작성할 필요가 없다. 기본 입력은 Body, Query 또는 없음이다. Path·추가 Query·Header는 추가 입력 위치에서 지정한다. Path의 속성 이름은 URL의 `{변수}`와 일치하고 required에 포함되어야 한다. Body/응답에는 기존 지원 범위의 schema를 그대로 넣는다. Query/Path/Header에는 type=object, properties, required로 이름별 schema를 작성한다. 해당 매개변수 묶음의 추가 제약은 조용히 버리지 않고 거절한다.

요청 URL에는 query·fragment·비밀번호를 넣지 않는다. query 값은 시험 입력에서 받고, 인증은 연결의 서버 비밀 참조로 지정한다. 성공 204는 응답 schema가 없다. 202·multipart·스트리밍 등은 기존 A04와 같이 지원하지 않는다. GET body는 지원하지 않는다. 준비 단계의 로컬 확인은 최종 schema 지원 판정이 아니며 저장 시 서버가 다시 검사한다.

직접 입력한 전체 주소를 origin과 경로로 분리한다. 선택한 ConnectionVersion의 base URL과 경로를 조합한 실제 주소가 입력 주소와 일치해야 저장한다. `/v1` 같은 접두어가 두 번 붙거나 다른 서버로 바뀌는 것을 막는다. 직접 등록에서 새 연결의 주소는 입력한 origin으로 고정한다. 기본 외부 효과는 write이며 조회 전용을 확인한 경우에만 고급 설정에서 read로 변경한다.

## 공통 구현과 소유권

직접 입력을 단일 operation의 OpenAPI 3.1 문서로 조합하고 기존 import/compiler로 검증한다. 생성된 operationId `agent_console_direct_v1`은 UI가 새 버전 편집 시 직접 등록 양식을 복원하는 표시이며 권한·실행 정책에는 사용하지 않는다. 문서 가져오기와 동일한 `openapi_http_v1` ToolVersion·HTTP runtime·REST/MCP·시험 성공 후 게시 계약을 사용한다. 문서/Tool/버전/실행 기록은 Agent DB에 있으며 새 DB migration이나 다른 플랫폼 의존성을 추가하지 않았다.

Schema JSON의 숫자 token은 원문을 사용하며 큰 정수를 float64로 바꿔서 저장하지 않는다. 중복 키·비정상 숫자·과도한 크기/깊이를 거절하고 추가 매개변수의 schema도 원문으로 조합한다. 인증 원문은 생성된 OpenAPI에 포함하지 않는다. 직접 등록의 문서 저장 응답 유실도 기존 POST 멱등 키로 재확인한다. 이 기록은 이미 성공한 import를 다시 쓸 필요가 없는 경우 재사용하며, 두 번째 ToolVersion 저장 단계의 기존 복구 계약도 유지한다.

두 방식 모두 진입·방식 변경·다음 단계·예시 채우기·시험 입력 불러오기는 실제 대상 API를 호출하지 않는다. 시험/실행 버튼을 눌러야 호출한다. 업로드한 시험 JSON 원문은 바꾸지 않고 전송한다.

## Knowledge 검색으로 시험하기

현재 `POST /api/indexes/{index_id}/search`는 **질문 임베딩 벡터를 받는 동기 조회 API**다. `query_text`만 보내서는 검색할 수 없다. 질문→Model query embedding→Knowledge 검색은 기존 Knowledge 화면이 조립하는 순서다. 자연어만 받는 Agent 내장 검색 Tool은 A05이며 이번 일반 API 등록 시험과 구분한다.

기존 `test1`의 현재 active version을 조회하여 사용한다. 이전 세션의 UUID·embedding space를 그대로 고정값으로 쓰지 않는다. 사용자가 `--index-id`·`--index-version-id`를 지정하면 해당 버전을 확인하고 고정한다. ready·held·색인 완료 여부와 배포의 공간/차원/ready/L2를 확인한 뒤, Knowledge Gateway의 기존 Model 전달 경로로 **질문 임베딩 한 번**을 요청한다. 모델 배포·정제·청킹·재색인은 수행하지 않는다.

### 1. 새 시험 입력이 필요한 경우에만 준비

사용자 서버의 `agent_platform`에서 실행한다. 현재 사용자 로그에서는 아래 작업이 이미 성공했으므로 재실행할 필요가 없다. 다른 질문으로 새 입력을 준비한다면 `--out`을 아직 없는 새 디렉터리로 바꾼다. 기존 폴더는 자동으로 덮어쓰지 않는다.

```bash
cd ~/workspace/agent_project/agent_platform
python3 ops/prepare_knowledge_tool_test.py \
  --index-name test1 \
  --question 'AI 엔지니어에게 필요한 역량은 무엇인가요?' \
  --check-search \
  --out ~/workspace/knowledge_tool_test_20260923
```

기본 `--knowledge-url http://127.0.0.1:15100`은 **스크립트를 실행하는 호스트에서 접근하는 주소**다. `--tool-base-url http://knowledge-gateway:8000`은 **Agent Runtime 컨테이너에서 접근하는 주소**다. 기존 기본 Compose는 Agent와 Knowledge가 같은 ops-infra 네트워크를 사용한다. 환경이 다르면 두 옵션을 각각 접근 가능한 주소로 지정한다. Tool에는 localhost를 등록하지 않는다. 현재 소스의 Knowledge Gateway는 별도 인증을 요구하지 않으므로 예시 연결은 인증 없음이다.

`--check-search`는 같은 벡터로 검색 API를 직접 한 번 호출하고 index/version/space·개수·rerank off 응답을 확인한다. 결과가 0개여도 정상 검색 응답일 수 있다. 이 검사는 아직 Agent Tool 실행 성공을 뜻하지 않는다. 옵션을 빼면 임베딩과 시험 입력 생성까지만 수행한다. 재정렬은 끈다.

출력 파일:

| 파일 | 용도 |
|---|---|
| tool-arguments.json | `{path:{index_id}, body:{index_version_id, embedding_space_id, query_embedding, top_k, candidate_k}}` 시험 입력 |
| request-schema.json | 직접 등록의 Body schema |
| response-schema.json | 직접 등록의 응답 schema |
| path-schema.json | 추가 입력 위치의 Path schema |
| openapi.json | 동일한 검색 API를 OpenAPI 방식으로 등록할 때 업로드 |
| preparation-result.json | 사용한 버전/배포·벡터 준비·선택적 직접 검색 확인의 작은 요약 |

벡터는 시험 입력 파일에만 저장하고 질문 원문·원문 검색 결과는 요약에 넣지 않는다. 결과 폴더는 0700, 파일은 0600이다. 기존 폴더가 있으면 호출 전에 중단하며 자동 재시도는 하지 않는다.

### 2. 두 등록 경로 중 하나로 시험

**직접 등록:** 도구 추가 → API 직접 등록 → `지식플랫폼 검색 API로 시험하기` → `검색 API 예시 채우기` → 다음. 예시의 요청 URL은 `http://knowledge-gateway:8000/api/indexes/{index_id}/search`다. 다른 tool-base-url을 썼으면 화면 주소도 맞춘다. 새 서비스 연결에서 주소를 확인하고 인증 없음으로 저장한다. 도구 이름은 `지식 검색`으로 정하고 고급 설정의 외부 효과를 `조회만 수행함을 확인함`으로 선택한 뒤 저장한다.

**OpenAPI:** 같은 준비 결과의 `openapi.json`을 로컬 PC로 내려받아 도구 추가 → OpenAPI 가져오기에서 올린다. 지식 검색을 선택하고 같은 주소/인증 없음 연결을 지정한다. 고급 설정에서 조회 전용을 확인한다. 두 방식을 각각 비교하려면 도구 이름에 직접/OpenAPI 구분을 붙인다. 시험 입력은 동일한 파일을 사용한다.

**공통 시험:** `tool-arguments.json`을 로컬 PC로 내려받아 시험·사용 화면의 `시험 입력 파일 불러오기`에서 선택 → `입력 불러오기` → `시험 실행`. 파일 불러오기 자체는 호출하지 않는다. 서버 파일을 복사하기 어렵다면 JSON 전체를 `JSON으로 직접 입력` 칸에 붙여 넣어도 된다. 실행 결과가 성공이고 `results`·본문/출처 등 응답이 있으면 확인한 뒤 `사용 시작`을 누른다. 원시 정보에서 ToolCall ID와 request ID도 확인한다.

이 단계는 등록·호출·응답 형식 검증이다. 검색 품질이나 LLM의 도구 선택을 평가하는 단계는 아니다. 정상 빈 결과는 질문/지식 내용을 함께 확인한다. 프로필 미준비·네트워크 오류·검색 schema 오류는 준비/실행 단계의 오류로 구분한다.

## 로컬 검증과 서버 적용

- 기존 콘솔 DOM/API 대역 검사 27개 묶음, 직접 등록/URL·schema·인증·숫자 보존·입력 파일·응답 유실·예시 검사 21개 묶음 통과.
- Python 시험 준비 검사 10개 통과. 프로필/벡터/상태 오류·중복 이름·private output·실제 loopback HTTP와 redirect 거절 포함. Knowledge/Model 응답은 대역이며 실제 배포를 호출하지 않았다.
- JS 문법·HTML 구조 검증 통과. 이 환경에 Go/Docker/실행 가능한 브라우저가 없어 새 화면 레이아웃·Gateway 이미지 빌드·실제 Agent/DB 통합 검사는 수행하지 않았다. 앞선 최초 A04의 과거 인수 결과와 합산하지 않는다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_tool_registration_20260923.tar.gz
python3 agent_platform_tool_registration_20260923/apply_patch.py \
  --project-root ~/workspace/agent_project --apply
cd ~/workspace/agent_project/agent_platform
docker compose up -d --build --no-deps --wait agent-gateway
)
```

위 코드는 직접/OpenAPI 기능을 처음 적용할 때의 절차다. 사용자의 최신 로그에서 이미 성공했으므로 현재 시험을 위해 다시 적용·재빌드하지 않는다. 기준은 사용자가 화면 변경을 확인한 `agent_platform_console_20260923` 적용 소스다. 파일 해시가 다르면 덮어쓰기 전에 중단하며 백업과 동일 패치 재적용을 지원한다. 기존 설정/비밀/DB/Model·Data·Knowledge·infra 실행 코드는 변경하지 않았다. 위의 화면 시험 성공을 확인했으며, 다음은 게시·발행 후 실행과 남은 A04 인수 후 A05 자연어 검색 Tool로 진행하는 것이다.
