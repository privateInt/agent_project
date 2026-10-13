# A07 후속: 화면 없이 실제 API 질문 검증

**2026-09-24 후속 확인:** Runtime 재빌드와 실제 Gemma 배포 후 A07-live 실제 질문이 통과했다. 기존 실패는 generate의 MODEL_NOT_READY이며 당시 LLM 미배포를 사용자가 확인했다. 아래 내용은 이전 구현·진단 절차로 유지한다. 현재 다음 작업은 [운영 n8n 등록·실행](n8n-operational-20260924.md)이며 이전 live 검사를 문서 확인 목적으로 반복하지 않는다.

**후속 수정:** 답변 모델 필터와 실패 진단은 [livefix 안내](../history/agent-platform-202609.md#n8n-livefix-20260924)를 따른다. 기존 live 패치는 운영 재빌드가 필요 없었지만, livefix의 서버 모델 필터 반영에는 Agent Runtime만 재빌드해야 한다. 기존 세션의 `diagnose`는 재빌드 없이 바로 사용한다.

## 범위와 현재 근거

`n8n/ops/live_check.py`로 질문과 기존 자원을 선택하고, 실제 Agent REST를 통해 임베딩 → 지식 검색 → 답변 생성 → 출처 반환을 검사한다. 실행기는 고정 n8n 2.40.5의 **별도 CLI 검사 컨테이너**다. 운영 n8n의 PostgreSQL·암호화 키·워크플로는 변경하지 않는다. Agent a07.1 / schema 5를 유지하며 운영 서비스 재빌드·재시작은 필요 없다. 첫 실행에는 이 검사 전용 이미지를 빌드한다.

사용자가 `n8n-verified` 문서 패치 14개 적용을 확인했다. 앞서 서버에서 확인한 infra n8n/PostgreSQL Healthy와 격리 검사 7개 통과는 유지한다. 이 후속 작업의 서버 실제 질문 실행은 아직 미확인이다. 페이지, 운영 n8n에서 사용자가 Flow를 편집·실행하는 과정, GPU 답변 품질, 서버 재시작 후 조회는 별도 인수로 남긴다.

## 서버 실행

Docker를 사용할 수 있는 기존 일반 사용자 계정으로 Agent Platform 폴더에서 실행한다. 기본 Agent API 토큰 파일 `.secrets/api-token`을 읽으며 터미널에 토큰을 입력하거나 출력하지 않는다.

```bash
cd ~/workspace/agent_project/agent_platform
python3 n8n/ops/live_check.py prepare
python3 n8n/ops/live_check.py run
```

`prepare`의 선택 순서:

1. 실제 색인된 자료에 관한 질문을 입력한다. 예: 해당 논문이 선택됐다면 “이 연구의 기능 단위와 시스템 경계는 무엇인가요?”
2. 사용 가능한 Agent가 있으면 번호로 선택하거나 **새 시험 Agent 만들기**를 선택한다. Agent가 없으면 검색 도구 선택으로 진행한다.
3. 새 Agent는 기존 **자연어 검색 도구**(`knowledge_search_v1`)와 준비된 답변 LLM을 번호로 선택한다. `A04 test endpoint` 같은 일반 HTTP 테스트 도구는 후보에서 제외한다. 실제 자료를 연결한 검색 도구를 고른다.
4. 검색 도구가 초안이면 발행 여부를 묻는다. `y`로 선택할 때만 발행하며, 서버에 기존 성공 시험 기록이 있어야 한다. 발행 실패 시 원래 도구를 시험한 뒤 같은 세션으로 다시 준비한다.
5. 선택 지식·답변 모델·질문·Agent 버전의 준비 요약을 확인한다. 새 Agent는 **초안**으로 만들며 자동 발행하지 않는다.

준비는 추론 API를 호출하지 않는다. 지식과 모델의 준비 상태 및 고정 버전이 일치하는지 조회한다. 준비된 답변 모델이 없으면 Model Platform에서 기존 vLLM LLM 배포를 준비해야 한다. 도구의 임베딩 Model 연결을 답변 모델 연결의 기본값으로 사용한다. 다른 연결을 쓰려면 `--model-connection-version-id`를 지정한다.

`run`은 실제 임베딩·검색·답변 추론을 요청한다. 같은 질문을 두 n8n 실행에서 재생하고, Agent가 동일 실행을 반환했는지 검사한다. 새로운 질문의 추론은 한 번이며 재생 요청은 원래 결과를 조회한다. 초안 Agent는 `mode: test`, 발행 Agent는 `mode: run`을 사용한다.

## 성공과 결과 확인

성공 요약의 기준은 `result: passed`, `outcome: answered`, `replay_verified: true`다. 그 아래 답변과 출처 청크·원문 페이지를 출력한다. 전체 원문 출처, 단계, 검색 결과, 모델·색인 식별자는 비공개 세션의 `result.json`에 있다.

검사 항목:

- AgentRun의 질문·버전·정의 해시·호출자·멱등 키 유지
- validate/search/context/generate/finalize 5단계와 검색 ToolCall의 부모 실행·request ID 연결
- 고정 색인 버전·임베딩 공간·임베딩 배포, 검색 호출 기록
- 답변 모델의 배포·모델 버전·사용량
- 답변 인용 표식과 검색 원문·페이지·품질 정보의 일치
- 서로 다른 n8n 실행 ID에서 동일 AgentRun·ToolCall·request ID·caller_context 반환 및 `Idempotency-Replayed` 응답

`no_evidence`는 정상적으로 처리된 실행이지만 답변·출처 전체 경로의 인수로 삼지 않는다. `result: incomplete`와 종료 코드 2를 반환한다. 선택 자료에 실제로 근거가 있는 질문을 **새 세션**에서 시험한다. `passed` 역시 답변 내용의 의미적 정확성은 보증하지 않는다(`semantic_quality: not_assessed`). 인용 자료를 읽고 질문에 맞는 답변인지 별도로 확인한다.

## 중단·재실행·재시작 후 조회

기본 세션은 프로젝트 밖 `~/workspace/agent_n8n_live_check`다. 질문과 토큰이 암호화 저장된 검사 SQLite, 암호화 키, 실행 기록이 들어 있으므로 폴더 0700·파일 0600을 유지한다. 이 폴더를 소스 압축·공유에 포함하지 않는다. 토큰은 컨테이너 표준입력으로만 전달하며 임시 평문 credential 파일은 가져오기 후 삭제한다. 원시 CLI 로그도 이 세션 안에만 저장한다.

```bash
# 중단되었거나 접수 응답을 받지 못한 경우: 원래 요청으로 복구
python3 n8n/ops/live_check.py run

# 접수 ID를 저장한 이후: 새 추론 없이 현재 결과만 조회
python3 n8n/ops/live_check.py lookup
```

요청을 디스크에 저장한 뒤 POST하므로 응답이 유실돼도 같은 세션의 `run`은 원래 키·본문·호출자를 재사용한다. `accepted.json`이 아직 없으면 `lookup`은 실행을 추측하거나 POST하지 않는다. 안내에 따라 `run`으로 복구한다. 실패·중단된 AgentRun은 같은 키로 새 실행하지 않는다. 원인 해결 뒤 새 세션에서 다시 시험한다.

시스템이 강제 종료돼 동일 이름의 검사 컨테이너가 남으면 Docker가 이름 충돌을 알릴 수 있다. 먼저 그 검사 컨테이너의 실행 여부를 확인한다. 아직 실행 중이면 기다리고, 종료된 컨테이너만 정리한 뒤 같은 명령을 다시 실행한다. 도구가 운영 컨테이너를 자동 삭제하지 않는다.

새 질문/새 자원은 별도 세션을 지정한다. 이미 준비한 세션의 입력·모델·도구는 바꾸지 못한다.

```bash
python3 n8n/ops/live_check.py prepare --session-dir ~/workspace/agent_n8n_live_check_2
python3 n8n/ops/live_check.py run --session-dir ~/workspace/agent_n8n_live_check_2
```

호스트 주소 기본값은 `http://127.0.0.1:16100`, 검사 컨테이너 주소는 `http://agent-gateway:8000`이다. 기본값이 다르면 준비 시 각각 `--agent-url`, `--container-agent-url`을 지정하고 준비 명령을 재시도할 때도 같은 옵션을 쓴다. Agent `.env`의 `AGENT_NETWORK`에 연결한다.

자동화할 때는 `--non-interactive --question '질문' --agent-version-id <ID>`를 사용한다. 새 Agent는 `--new-agent --tool-version-id <ID> --deployment-id <ID>`를 지정한다. 초안 도구를 발행하려면 `--publish-tool`도 명시한다. 토큰·인증정보를 URL이나 질문에 넣지 않는다.

## 로컬 개발 검증과 남은 작업

검증 요약은 `src/platform/agent/tests/results/a07-live-summary.json`에 보관한다. 실제 n8n CLI → 실제 Go Gateway/Service → Python Runtime → SQL 호환 PGlite를 연결하고, Model/Knowledge는 HTTP 테스트 대역을 쓴다. 서버의 Docker/PostgreSQL 16/GPU 검증과 구분한다.

로컬 Python 12개·Node 8개·기존 워크플로 계약 6개, 실제 n8n/Agent 연결 검사(45.22초)가 통과했다. 로컬 Python 12개·Node 8개·기존 워크플로 계약 6개, 실제 n8n/Agent 연결 검사(45.22초)가 통과했다. 준비 요청의 응답 유실·재개, 잘못된 자원 선택 차단, 발행 동의, 고정 식별자·출처 훼손 차단을 단위 검사한다. 실제 연결 검사에서는 Agent 접수 응답 3개를 모두 유실시킨 뒤 다음 프로세스에서 복구하고, 반복 실행·조회에서도 임베딩/검색/답변이 각각 1회인지 확인한다.

다음은 사용자 서버에서 위 질문 검사 로그를 받고 인수 범위를 갱신하는 작업이다. 이후 운영 n8n에 워크플로를 가져와 WorkflowLink 연결·편집·실행을 확인한다. 페이지 확인은 사용자가 돌아온 뒤 진행한다. 내부 schema 자동완성, 서비스 단위 API 묶음 관리는 기존 후속 요구로 유지한다.
