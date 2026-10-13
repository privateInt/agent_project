# Agent Platform 운영 안내 — A09

2026-09-24. 기존 플랫폼 경계와 고정 실행 계약을 기준으로 사용·점검·복구 진입점을 정리한다. 최신 인수 상태는 [A09 통합 인수](acceptance-a09-20260924.md), UI는 [A08](console-a08-20260924.md)를 따른다. 과거 단계 문서의 최초 설치/오류 복구 명령을 현재 정상 서버에 반복 실행하지 않는다.

## 소유권과 현재 구성

| 주체 | 소유하는 것 | 호출 경계 |
|---|---|---|
| Agent Platform | Tool/Agent/고정 버전/WorkflowLink/ToolCall/AgentRun/단계 | Gateway REST/MCP, 내부 Service↔Runtime gRPC |
| infra/n8n | n8n 런타임·PostgreSQL·계정·키·Flow·n8n 실행 | Agent Gateway 공개 API로 질문 접수/조회 |
| Model Platform | 모델 버전·배포·임베딩·채팅 | Agent가 Model 공개 API 사용 |
| Knowledge Platform | 지식·인덱스 버전·공간·검색 결과/출처 | 검색 Tool이 Knowledge 공개 API 사용 |
| Data Platform | 수집·정제·원문/canonical | 지식 생성 파이프라인의 입력 소유 |

Agent 컨테이너에 다른 플랫폼의 DB 계정·Docker socket·GPU 제어 권한을 추가하지 않는다. n8n은 상위 업무흐름을, Agent는 질문/검색/답변 실행을 맡는다. n8n Flow의 숨김·중지·게시와 Agent 비활성화는 서로 다른 동작이다.

현재 Service/Gateway 이미지 태그는 a08.1이며 core status는 a07.1/A07/schema 5다. Runtime은 인용 진단 패치의 a09.1이며 MCP/n8n은 기존 고정값을 유지한다. A09 점검 도구 추가로 전체 이미지 버전을 올리지 않는다.

## 현재 확인된 운영 세션

2026-09-25 전달·재사용 검증에 사용한 세션은 `~/.local/state/agent-platform/n8n-citation-20260925`다. 아래 일상 명령에는 이 `--session-dir`와 해당 `--request-file`을 명시한다. 기본 workspace 경로로 돌아가 세션이 없다고 판단하거나 Flow/키를 재등록하지 않는다. 정확한 읽기 명령과 기준 파일은 [최신 검증 기록](../history/agent-platform-202609.md#n8n-operational-verified-20260925)을 따른다.

## 일상 작업

| 작업 | 진입점 | 외부 실행/변경 |
|---|---|---|
| 현재 기록과 연결 점검 | `python3 -B ops/acceptance.py audit` | GET 조회만 |
| 비교 기준 생성 | `python3 -B ops/acceptance.py capture` | 별도 기준 파일 1개 생성; 원래 세션 유지 |
| 이전 기준과 비교 | `python3 -B ops/acceptance.py compare` | GET 조회만 |
| Tool·Agent·Flow·결과 관리 | `http://<서버IP>:16100/console` | 저장/질문 버튼별 변경·추론 안내 |
| n8n Flow 상태 | `python3 n8n/ops/operational.py status` | 원격 GET; 기존 CLI의 로컬 lock 사용 |
| n8n 기존 요청 결과 | `python3 n8n/ops/operational.py lookup` | 추론 없음; 기존 CLI는 로컬 조회/결과 캐시 갱신 |
| n8n 실제 질문/재전달 | `python3 n8n/ops/operational.py run` | 웹훅 호출; 아래 요청 재사용 규칙 적용 |

명령의 작업 폴더는 `~/workspace/agent_project/agent_platform`이다. n8n 계정/키 설정 도구는 프로젝트 루트에서 `python3 infra/n8n/ops/api_access.py`로 실행한다. 이미 연결된 서버에서 불필요하게 계정/키를 다시 만들지 않는다.

### 새 질문과 기존 요청의 차이

기존 `request.json`으로 run을 반복하면 n8n 실행 이력은 추가될 수 있지만 AgentRun·입력·호출자·결과는 재사용한다. 기존 실패가 새 LLM 추론으로 바뀌지 않는다. 같은 파일에 다른 질문을 넣으면 거절한다.

사용자가 실제 새 질문을 실행하려는 경우에만 별도 요청 파일과 질문을 지정한다. 예:

```bash
python3 n8n/ops/operational.py run \
  --request-file ~/workspace/agent_n8n_workflow/question-002.json \
  --question '자료에서 환경영향 평가의 범위를 설명해줘'
```

이 명령은 실제 모델/지식 API를 호출한다. 현재 A09 읽기 점검을 위해 실행할 필요 없다. 성공/실패 여부와 무관하게 요청 파일을 유지하고, 결과 확인은 같은 `--request-file`을 붙인 lookup으로 한다. 성공이 나올 때까지 새 요청을 자동 생성하지 않는다.

## 장애별 확인 순서

| 증상 | 우선 확인 | 유지할 것 |
|---|---|---|
| Agent 연결/ready 실패 | Agent Compose ps, 관련 서비스 로그, 토큰 경로·포트 | DB 볼륨·기존 비밀 |
| 모델 미준비/식별자 불일치 | 실패 단계·고정 배포/모델 버전, Model Platform 배포 상태 | 원래 실패 기록; Agent에서 임의 다른 모델 대체 금지 |
| 검색 실패 | ToolCall의 원래 오류·지식/공간/인덱스 버전 | 기존 지식/인덱스, 다른 DB 직접 변경 금지 |
| ANSWER_REFERENCE_INVALID | finalize 단계·원래 기록, 품질 후속 | 검증 유지; 임의 인용 보충·무근거 성공 처리 금지 |
| 웹훅 응답 불확실 | 원래 요청 파일로 조회, AgentRun 접수 여부 | request.json·멱등 키; 무조건 새 요청 금지 |
| n8n 등록 응답 불확실 | 같은 prepare의 조회 복구, 기존 등록 이름·ID | settings/journal; Flow/키 자동 재생성 금지 |
| Flow 정의/게시 버전 불일치 | n8n에서 실제 변경 여부 확인 | 기존 내용을 자동 덮어쓰지 않음 |
| A09 changed | 변경된 runs/n8n/접속 origin 구분, 의도한 설정 변경과 데이터 소실 구분 | 비교 기준을 삭제해 통과로 만들지 않음 |
| 오래된 n8n 실행 조회 실패 | n8n 실행 보관/prune 정책·권한 | Agent 결과와 n8n 전달 증거의 차이를 명시 |

Agent 로그 확인은 `docker compose logs --tail=100 agent-service agent-runtime agent-gateway`로 시작한다. n8n 인프라 진단은 프로젝트 루트의 `python3 infra/n8n/ops/diagnose.py`를 사용한다. 원문 로그에 질문·문서·답변·인증값이 포함될 수 있으므로 공유용으로는 A09의 ID/상태/오류 요약을 우선 사용한다.

## 설치·갱신·재시작

- 신규 환경은 [A04 기본 실행](openapi-foundation.md)과 [A06](knowledge-qa-a06-20260924.md), [n8n infra](n8n-infra-20260924.md)의 역할/설정을 따른다. 현재 전체 소스의 Compose/설정 생성기를 사용하고 과거 a02/a03 이미지로 되돌리지 않는다.
- 기존 서버는 전달 패치의 README를 따른다. 설정/비밀/DB 보존 및 전체 사전 hash 검사 뒤 적용한다. A09는 호스트 도구이므로 재빌드·재시작이 없다.
- n8n 기동/복구 주체는 infra다. 기존 루트 명령 `python3 infra/ops/up.py --n8n-only`를 사용하며 Agent Compose에 n8n을 다시 편입하지 않는다.
- 서비스 교체는 실행 요청 유입을 통제하고 Agent/Tool/n8n 작업 완료를 확인한 유지보수 창에서 한다. 진행 중 worker 중단은 interrupted가 될 수 있다. 큐의 자동 재실행/보상 처리로 간주하지 않는다.
- 재시작 보존 확인은 A09 capture → 실제 수행한 재시작 로그 → compare를 함께 사용한다. compare만으로 실제 재시작을 증명할 수 없다.

## 보관과 복구 경계

소스 tar와 코드 패치의 `.patch-backups`는 DB 백업이 아니다. 복구에 필요한 자산은 Agent DB, n8n DB/데이터·암호화 키, 플랫폼 설정/자격, 프로젝트 밖 운영 세션과 요청 파일이다. 암호화 키 없이 n8n DB만 복원하면 자격을 사용할 수 없을 수 있다. 비밀/운영 상태를 소스 전체 압축에 포함하지 않는다.

백업 절차의 실제 복원·키 회전·보관 기간 설정은 별도 운영 인수다. A09는 백업을 실행하거나 검증되지 않은 복원을 완료 처리하지 않는다. 볼륨 삭제, 계정 초기화, 비밀 재생성으로 점검 오류를 우회하지 않는다.

소스에 포함하는 테스트 결과 합계는 1 MiB 미만을 유지한다. 서버의 요청/DB 운영 기록은 이 소스 배포 예산과 구분한다. 기준 JSON은 원문 대신 해시를 보관하지만 실행 ID와 질문/결과의 지문이 있으므로 사용자 전용 0600으로 보관한다.

## 완료 기준과 후속

A09의 도구 구현/로컬 검증과 사용자 서버 실제 인수를 구분한다. 새 Flow의 운영 전달·동일 요청 재사용·기준 비교는 [서버 검증](../history/agent-platform-202609.md#n8n-operational-verified-20260925)에서 통과했다. 과거 인용 실패는 보존하며, 새 답변의 질문 반복은 품질 문제다. [Agent/n8n 7개 컨테이너의 재시작 보존](../history/agent-platform-202609.md#restart-verified-20260925)은 2026-09-25에 통과했다. 사용자 화면·의미 품질·백업 복원 및 다른 플랫폼 재시작 범위는 별도다. 의미 품질 개선은 별도 목록으로 보류한다. A10 전체 플랫폼 모니터링은 이 상태를 근거로 범위를 결정하며 이번 작업에서 모니터링 인프라를 설치하지 않는다.

## 로컬 운영 세션이 없을 때

`prepare` 재등록에 앞서 [기존 세션 복구 절차](../history/agent-platform-202609.md#n8n-session-recovery-20260925)를 따른다. 운영 API 키와 동일 서버 n8n 관리 CLI 권한, 보존된 완료 실행 및 고정된 Flow가 필요하다. `recover.py inspect`는 GET만 수행하며, `restore`는 해당 Flow의 두 자격만 관리 CLI로 읽고 프로젝트 외부의 0700/0600 세션에 저장한다. 기존 Flow·연결·자격을 생성/수정/게시하거나 모델을 실행하지 않는다. 복구 세션에는 prepare를 허용하지 않는다. 기존 실패 요청은 같은 실패 기록으로 조회되며 새 시험 요청은 별도 파일로 만든다.
