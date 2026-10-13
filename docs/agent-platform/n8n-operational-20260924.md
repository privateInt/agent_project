# A07 운영 n8n 워크플로 — 등록·실행·조회

## 최신 서버 결과 — 연결·등록 성공, 인용 검증 실패

2026-09-24 사용자 터미널 결과를 반영했다. 이번 기록은 운영 n8n HTTP 워크플로의 실제 실행이며 이전 격리 CLI의 A07-live 성공과 구분한다.

| 항목 | 확인 결과 |
|---|---|
| accessfix 적용 | Already applied |
| 등록 전 진단 | setup_required=true, 최소 길이 8, 기존 키·pending 없음 |
| 최초 계정/API 키 | connected, reused=false, n8n 2.40.5, password_saved=false |
| 운영 준비 | prepared, webhook_authenticated=true, inference_executed=false |
| AgentVersion | `6f0fb4b9-2796-4e33-b040-cc6149bd0988`, prepare에서 발행 확인 |
| n8n workflow | `HtgqKREU0RNPfSnB` |
| WorkflowLink | `e9e79c18-9573-4772-af7f-80f410fe634c` |
| n8n execution | `1` |
| 논리 요청 ID | `90868741-04b7-4978-afcb-3db4253f0f50` |
| AgentRun | `15981e7e-7ab3-41d6-bef8-ab2924cbe67e` |
| Agent request ID | `93218149-3ad0-45f7-81b2-48f0f72f5bcf` |
| ToolCall | `39eac542-0c91-4975-b2d6-434d9ad49e2e` |
| 단계 | validate/search/context/generate=succeeded, finalize=failed |
| 최종 결과 | failed, ANSWER_REFERENCE_INVALID, stage=finalize |

계정 연결 HTTP 400 문제는 이번 시도에서 해소됐다. 과거 HTTP 400의 정확한 원인을 비밀번호로 소급 확정하지 않는다.

Runtime은 모델 응답의 `[S1]` 형식 참조를 확인한다. 인용이 없거나 형식이 맞지 않거나 선택된 문맥 범위 밖 ID가 있으면 위 오류를 반환한다. 생성 답변 원문은 이번 로그와 기존 generate 단계 기록에 없으므로 세 경우 중 하나로 단정할 수 없다. 원본 답변을 보지 않고 인용을 붙이거나 출처 검증을 끄거나 실패를 성공/no_evidence로 바꾸지 않는다. 모델 자동 재호출도 하지 않는다. 인용 형식 준수는 [품질 후속](answer-quality-followup-20260924.md)에 추가했으며, 답변 전달 성공 인수의 미충족 조건으로 유지한다.

`webhook 응답을 확인하지 못해`라는 기존 문구는 HTTP 오류도 포함하는 공통 표시다. Agent 실패 시 현재 워크플로가 HTTP 502를 반환하는 경로가 있으므로 이 문구만으로 통신 장애라고 판단하지 않는다. 실제 로그는 첫 run의 내부 조회가 같은 Agent 실행과 실패 원인을 찾았음을 보여준다. 이 로그만으로 n8n 완료 이력의 최종 status까지 별도 검증됐다고 기록하지 않는다.

첫 run이 실패 종료 코드 2를 반환해 `set -e`가 명령 묶음을 중단했다. 두 번째 run과 마지막 별도 lookup은 미실행이다. 동일 요청 파일은 동일 AgentRun을 반환하므로 같은 run을 다시 실행해도 답변을 새로 생성하지 않는다. 새 추론은 새로운 요청 파일로 별도 실행하는 기능이지만, 이번 문서 정리를 위해 추가 추론하거나 성공이 나올 때까지 반복하지 않는다. `~/workspace/agent_n8n_workflow`와 기존 request.json을 유지한다.

필요한 경우 아래 명령으로 새 추론 없이 상태·기록을 확인할 수 있다. lookup은 기존 failed 결과를 그대로 출력하며 종료 코드 2가 정상이다.

```bash
cd ~/workspace/agent_project/agent_platform
python3 n8n/ops/operational.py status
python3 n8n/ops/operational.py lookup
```

현재 인수 범위: 계정 연결·운영 Flow 등록/게시·Agent 실행 접수·검색·모델 응답 수신·실패 기록 조회는 확인했다. 운영 Flow의 검증된 답변/출처 전달·동일 요청 재실행·서버 재시작 보존·페이지 확인은 미완료다. 실행 코드 변경 없이 문서·요약만 갱신했다. 근거: `agent_platform/tests/results/a07-operational-server-20260924.json`.

## 이전 이력 — 계정 연결 HTTP 400 후속 (최신 시도에서 해소)

사용자 서버는 운영 패치가 `Already applied`인 상태에서 `api_access.py`가 HTTP 400으로 종료됐다. 이전 도구가 실패 단계와 검증 필드를 숨겼으므로 비밀번호 오류라고 확정할 수 없다. 계정 등록 자체가 성공하고 API 키 발급에서 거절됐을 가능성도 남아 있다. 이후 prepare/run/lookup은 실행되지 않았다.

후속 `agent_platform_n8n_accessfix_20260924.tar.gz`를 적용한다. 기존 운영 패치 적용본이 기준이며 재빌드·재시작·LLM 재배포는 필요 없다. 계정·키·설정·볼륨·프로젝트 밖 실행 세션을 보존한다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_n8n_accessfix_20260924.tar.gz
python3 agent_platform_n8n_accessfix_20260924/apply_patch.py \
  --project-root ~/workspace/agent_project --apply
cd ~/workspace/agent_project
python3 infra/n8n/ops/api_access.py --diagnose
python3 infra/n8n/ops/api_access.py
)
```

- `--diagnose`는 공개 설정을 GET으로 조회하고 등록 필요 여부·로컬 키/미확정 발급 기록의 존재만 표시한다. 로그인·키 발급·파일/락 생성·추론을 하지 않으며 계정 정보와 키 내용을 출력하지 않는다. 키 파일 존재가 키 유효성을 뜻하지는 않는다.
- `setup_required=true`이면 최초 등록, false이면 기존 계정 로그인이다. 이전 시도에서 등록이 됐다면 같은 계정으로 로그인한다. 저장된 키가 있으면 검증·재사용한다.
- 최초 등록 비밀번호는 기본 8~64자, 영문 대문자(A–Z)·숫자(0–9) 각각 1개 이상이다. 서버가 더 긴 최소 길이를 설정했다면 그 값을 안내·검사한다. 특수문자는 필수가 아니다. n8n과 동일하게 UTF-16 길이로 검사한다. 한글 이름·성을 허용하며 기존 로그인 비밀번호에 신규 등록 정책을 강제하지 않는다.
- 오류에 `owner_setup`(계정 등록), `login`(로그인), `api_key_create`(키 발급), `settings`(설정 조회) 단계를 표시한다. 서버 검증 오류는 허용된 필드·규칙만 추출하고 원문 응답·입력값·비밀번호·키·traceback은 출력하지 않는다. 알 수 없는 오류는 단계/HTTP 상태만 표시한다.
- 키 발급 응답 유실 시 기존 pending 보존 규칙을 유지한다. 해당 경우 자동 재발급이나 계정 초기화로 우회하지 않는다.

연결 결과가 `connected`이면 아래 3~4절의 prepare/run/lookup을 이어서 실행한다. 새 `FAIL:` 줄은 공유할 수 있지만 비밀번호·API 키·비밀 파일 내용은 공유하지 않는다.

후속 로컬 검증: infra 단위 검사 26개(신규 연결 검사 10개 포함)와 Agent n8n 운영 도구 검사 3개가 통과했다. 실제 n8n 2.40.5 HTTP/새 격리 SQLite에서 최소 길이 16, 비밀번호 오류 3종, 한글 이름, 등록 성공 후 키 발급 HTTP 400, 기존 계정 로그인 복구, 제한된 키 발급·재사용을 확인했다. Model/Knowledge 호출과 사용자 운영 DB 접근은 없었다. Docker/PostgreSQL 환경의 사용자 재시도는 아직 미확인이다. 근거: `agent_platform/tests/results/a07-n8n-accessfix-summary.json`.

2026-09-24. 운영 n8n HTTP API로 문서 질의응답 워크플로를 저장·게시하고 질문을 실행하는 호스트 CLI를 추가했다. 로컬 실제 n8n 서버 검사와 실제 Agent Go/Python/SQL 연결 검사는 통과했다. 사용자 서버의 계정 연결·운영 등록·첫 실행을 확인했으며 인용 검증 실패로 실제 답변 전달 인수는 남아 있다.

Agent는 `a07.1` / DB schema 5를 유지한다. 이번 패치는 호스트 운영 도구·검사·문서만 변경하므로 Agent 이미지 재빌드나 기존 서비스 재시작이 필요 없다. n8n 계정·자격·워크플로는 n8n이, Tool/Agent 정의·버전·실행 기록은 Agent가 소유한다.

## 직전 서버에서 확인한 실제 질문 성공

사용자 첨부 `붙여넣은 텍스트 (1)(20260924-104323).txt` 기준이다. 상시 운영 n8n 워크플로 검증과 구분한다.

| 항목 | 확인값 |
|---|---|
| 검사 경로 | 별도 n8n CLI/격리 SQLite → 실제 Agent REST → 실제 Model/Knowledge |
| 결과 | A07-live passed, mode=test, outcome=answered |
| AgentRun | `dd6fd5e7-8188-45e5-a2a1-7c44375bf043` |
| AgentVersion | `6f0fb4b9-2796-4e33-b040-cc6149bd0988` (초안) |
| 요청 ID | `940c66da-aa68-482d-9156-dc9c65da358d` |
| ToolCall | `f9bd7229-0d70-4ea5-8514-d106976c61d2` |
| 답변 모델 | gemma3-v1, 배포 `537ea34d-b066-40dd-b08a-dbabec066da9` |
| 결과·재사용 | 검색 5개, 인용 2개, replay_verified=true, 잘림 없음, 경고 없음 |
| 품질 | semantic_quality=not_assessed; 답변 2·3번 항목 중복 관찰 |

이전 실패 AgentRun `42762502-b93d-46bb-b431-cd66d3b5e741`는 validate/search/context 성공 후 generate에서 MODEL_NOT_READY였다. 사용자가 당시 LLM 미배포를 확인했고 이후 실제 Gemma 배포·Gateway chat 성공과 Runtime 재빌드 후 위 질문 검사가 통과했다. 기존 실패 기록은 보존한다. 기본 live 세션 폴더가 없었던 이유는 미확정이며, 삭제 원인을 추정하지 않는다.

## 1. 최초 운영 패치 적용 (이미 적용했다면 위 후속 패치 사용)

`agent_platform_n8n_operational_20260924.tar.gz`를 `~/workspace`에 내려받는다. livefix 패치가 적용된 현재 소스가 기준이며 다른 수정과 충돌하면 쓰기 전에 중단한다. 설정·비밀·DB 볼륨·프로젝트 밖의 live 세션은 대상이 아니다.

```bash
(
set -e
cd ~/workspace
tar -xzf agent_platform_n8n_operational_20260924.tar.gz
python3 agent_platform_n8n_operational_20260924/apply_patch.py \
  --project-root ~/workspace/agent_project --apply
)
```

## 2. n8n 계정 연결 — 화면 없이 진행

```bash
cd ~/workspace/agent_project
python3 infra/n8n/ops/api_access.py
```

기본 주소는 서버의 `http://127.0.0.1:16200`이다. 포트를 바꿨다면 `--url`로 지정한다. 원격 n8n에 연결할 때는 HTTPS를 사용한다.

- 처음 사용하는 n8n이면 이메일·이름·성·비밀번호를 입력해 최초 소유자 계정을 등록한다. 비밀번호에는 n8n의 강도 조건을 적용하며 기존 계정을 초기화하지 않는다.
- 계정이 이미 있으면 기존 소유자/관리자로 로그인한다. MFA를 사용하는 계정은 코드를 입력한다. SSO 정책과 권한 거절은 서버 판단을 따른다.
- 비밀번호와 로그인 쿠키는 파일에 저장하지 않는다. 연동용 API 키만 infra의 `.secrets/agent-integration-api-key.json`에 0600으로 저장한다. 이후 같은 명령은 기존 키를 확인·재사용한다.
- 정상 계정 인증 및 키 발급에는 고정 n8n 2.40.5의 `/rest/owner/setup`, `/rest/login`, `/rest/api-keys`를 사용한다. 이후 자원 등록·실행 이력은 공개 `/api/v1` API로 처리한다. 내부 계정 API는 버전 의존성이 있으므로 n8n 버전 변경 시 재검증한다.
- API 키에는 워크플로 생성/조회/게시/게시 중지, 자격 생성/조회, 실행 조회 범위를 요청한다. 실제 권한 적용은 n8n 에디션과 계정 정책에 따른다.

API 키 발급 응답이 유실되면 pending 기록을 남기고 새 키를 자동 발급하지 않는다. pending 파일의 label로 해당 계정의 키 목록을 확인해야 한다. 일반 400/401/403 등 명확한 요청 거절은 불확실한 생성과 구분한다. 비밀번호·키·settings.json 원문을 대화나 로그에 붙이지 않는다.

## 3. 성공한 Agent로 운영 워크플로 준비

```bash
cd ~/workspace/agent_project/agent_platform
python3 n8n/ops/operational.py prepare \
  --agent-version-id 6f0fb4b9-2796-4e33-b040-cc6149bd0988 \
  --publish-agent
```

`--publish-agent`는 성공한 초안을 발행한다는 선택이다. 서버의 기존 성공 시험 조건을 그대로 적용한다. 이미 발행됐다면 재발행하지 않는다. 새 Agent/LLM 배포를 만들지 않는다. 대신 이전 세션에서 버전을 읽으려면 `--from-live-session ~/workspace/agent_n8n_live_check_gemma`를 사용할 수 있다.

준비 단계는 Agent를 발행하고 n8n 자격 2개·워크플로 1개를 등록·게시한 뒤 WorkflowLink를 저장한다. 질문·임베딩·검색·LLM 호출은 수행하지 않는다. 워크플로는 인증된 요청을 기다리며 예약 실행이나 외부 메시지 발송을 추가하지 않는다.

기본 상태 폴더는 `~/workspace/agent_n8n_workflow`다. `--session-dir`로 바꾸면 이후 명령도 같은 경로를 사용한다. 상태는 0700/0600으로 보관하며 패치나 소스 압축에 포함하지 않는다. 이 폴더를 삭제하면 등록된 자원과 요청의 복구 연결을 잃는다.

컨테이너에서 Agent로 가는 주소는 기본 `http://agent-gateway:8000`이며 `--container-agent-url`로 바꿀 수 있다. 호스트 Agent 주소는 `--agent-url`이다. 다른 주소를 사용했다면 prepare 재실행에도 동일한 주소를 지정한다. 화면에 표시할 n8n 주소를 지정하려면 처음 prepare에 `--instance-url http://<서버IP>:16200`을 추가한다. 이는 편집 링크용이며 실제 CLI 인증은 loopback/HTTPS 주소로 수행한다.

같은 prepare 재실행은 저장한 ID를 재사용한다. n8n 생성 API에는 Agent와 같은 멱등 키 계약이 없으므로 생성 전에 고유 이름과 진행 상태를 저장하고, 응답 유실 시 해당 이름으로 조회해 복구한다. 등록 여부가 여전히 불명확하거나 중복 이름이 발견되면 새 POST·임의 삭제를 하지 않는다. 기존 워크플로 내용이 바뀌면 덮어쓰지 않고 중단한다.

## 4. 질문 실행·재실행·조회

```bash
python3 n8n/ops/operational.py run \
  --question '환경영향 평가에 대해 알려줘'

# 같은 요청 파일: n8n 실행 이력은 새로 생기고 Agent 실행은 원래 것을 재사용
python3 n8n/ops/operational.py run

# GET만 수행: 질문이나 모델 실행을 새로 접수하지 않음
python3 n8n/ops/operational.py lookup
python3 n8n/ops/operational.py status
```

`run`은 기본 세션의 request.json에 질문·논리 요청 ID를 먼저 저장한다. webhook 응답이 유실되어도 같은 파일로 다시 호출한다. 다른 질문에는 새 요청 파일을 사용한다.

```bash
python3 n8n/ops/operational.py run \
  --question '이 자료에서 설명한 연구 범위는 무엇인가요?' \
  --request-file ~/workspace/agent_question_02.json
python3 n8n/ops/operational.py lookup \
  --request-file ~/workspace/agent_question_02.json
```

호출 입력은 `request_id`, `question` 두 필드뿐이다. AgentVersion/URL/mode는 서버에 저장한 워크플로에 고정하고 외부 입력으로 바꿀 수 없다. 같은 request_id는 Agent의 invocation_key가 되고 caller_context도 그대로 재현한다. caller_context.execution_id는 `request:<논리 요청 ID>`이며 실제 n8n execution ID들은 별도 이력에 기록한다. 같은 키로 질문을 바꾸면 거절한다.

webhook에는 본문 해시·대상 워크플로·AgentVersion에 묶인 120초 JWT를 사용한다. 장기 서명키와 Agent API 토큰은 워크플로 JSON/실행 입력에 포함하지 않는다. n8n이 webhook 헤더를 실행 이력에 보관할 수 있으므로 장기 Bearer 토큰을 inbound 인증으로 사용하지 않는다. 실행 이력의 단기 JWT로 다른 질문을 접수할 수 없고, 같은 질문 재전송은 기존 Agent 실행을 재사용한다. 서명키는 n8n 자격과 운영 세션에 보관한다.

`passed`는 n8n 완료 이력의 답변·출처·식별자와 Agent 저장 결과가 일치할 때만 표시한다. `no_evidence`는 정상 완료로 구분한다. 답변 품질 합격을 뜻하지 않는다. 두 번 완료한 뒤 `replay_verified=true`와 같은 AgentRun/ToolCall/request ID가 확인되어야 한다. webhook 응답과 이력 DB 반영 사이의 짧은 지연은 GET 재조회로 처리한다.

실패 시 Agent의 원래 code/stage와 단계 상태를 표시한다. 접수 ID가 전혀 조회되지 않으면 외부 효과 유무를 `unknown`으로 남긴다. HTTP 오류가 났다고 미실행으로 판단하지 않는다. 완료 기록이 아직 없으면 pending/incomplete와 종료 코드 2를 반환한다. failed도 종료 코드 2다. 같은 요청 파일을 보존하고 lookup으로 확인한다. 같은 run 재전송은 접수 응답 유실을 복구할 수 있지만, 종결된 Agent 실패를 새 추론으로 바꾸지는 않는다. n8n 이력 보관 기간이 지나 전달 이력을 확인할 수 없으면 Agent 결과만으로 n8n 전달 성공을 만들어내지 않는다.

## 5. 게시 중지와 보존

```bash
python3 n8n/ops/operational.py disable
python3 n8n/ops/operational.py lookup
```

disable은 이 세션의 n8n 워크플로 게시만 중지한다. Agent 발행·WorkflowLink·기존 실행 기록은 보존한다. 링크 enabled와 n8n 게시 상태는 별개이며, 재개하려면 동일한 prepare를 실행한다. 다른 n8n Flow를 변경하지 않는다.

운영 재시작 후에는 먼저 status/lookup으로 저장된 워크플로와 결과 보존을 확인한다. 실제 서버 재시작 검증은 아직 미수행이며 작업 중인 다른 Flow가 없는 시점에 별도 진행한다.

## 검증 근거와 남은 인수

- 로컬 실제 n8n 2.40.5 HTTP 서버·격리 SQLite·Agent HTTP 대역 검사 8개: 계정 연결, 생성 응답 유실 복구, 인증·비밀 경계, 접수 응답 유실 및 재실행, 질문 변경 거절, no_evidence/실패, n8n 프로세스 재시작 보존, 게시 중지 후 조회.
- 로컬 실제 n8n HTTP → Agent Go Gateway/Service → Python Runtime → PGlite SQL 통합: CLI prepare/run 반복, 동일 실행 기록 유지, 조회/게시 중지 통과. 임베딩·검색·채팅 대역 호출은 각각 1회 증가했다. TestAgentEndToEnd 17.19초 PASS.
- 기존 n8n 운영 도구 단위 검사 14개 통과. 새 Docker Compose의 빌드 경로·내부 네트워크·포트/운영 볼륨/비밀 미연결을 정규화해 확인했다.
- 로컬에는 Docker daemon/GPU가 없어 새 검사 Docker 이미지를 실행하지 못했다. 이후 사용자가 실제 서버의 계정 연결·운영 등록·첫 실행/인용 검증 실패를 확인했다. 위 로컬 테스트와 최신 사용자 서버 근거를 구분하며 운영 답변 성공으로 기록하지 않는다.
- 페이지 편집·사용자 Flow 변경 인수는 보류한다. 답변 품질은 [후속 목록](answer-quality-followup-20260924.md)에 따라 별도 개선한다. 다음 기능 단계는 A08 UI 통합이다.

선택적인 서버 격리 기능 검사는 다음과 같다. 새 격리 n8n만 띄우며 운영 DB/키/네트워크를 연결하지 않는다. 검사가 끝나면 테스트 컨테이너를 정리한다.

```bash
(
set -e
cd ~/workspace/agent_project/agent_platform
check_compose=n8n/tests/operational/docker-compose.yml
trap 'docker compose -f "$check_compose" down' EXIT
docker compose -f "$check_compose" up --build \
  --abort-on-container-exit --exit-code-from operational-checks
)
```

고정 버전의 n8n 공개 API OpenAPI 및 Webhook/JWT/계정/워크플로 구현과 대조했다. 공식 소스: [n8n 2.40.5](https://github.com/n8n-io/n8n/tree/n8n%402.40.5), [공개 API 명세](https://github.com/n8n-io/n8n/blob/n8n%402.40.5/packages/cli/src/public-api/v1/openapi.yml). 변동하는 최신 문서만으로 API 지원을 추정하지 않았다.
